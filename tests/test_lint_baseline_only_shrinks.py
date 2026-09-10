"""The lint baseline is a ratchet: four files may still carry an undefined name, and no more.

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

# The four files carrying a pre-existing undefined name on 2026-09-10. ONLY EVER REMOVE FROM THIS.
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


def test_the_selected_rules_are_still_the_correctness_ones():
    """A later edit that drops F821 from `select` would make the whole gate vacuous while every
    test here still passed — the baseline would be silencing a rule that no longer runs."""
    select = tomllib.loads(_RUFF.read_text())["lint"]["select"]
    assert "F821" in select, "the rule the baseline exists for is no longer selected"
    assert "E9" in select
