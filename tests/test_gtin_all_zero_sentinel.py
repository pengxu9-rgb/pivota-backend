"""Placeholder barcodes must not become global identity match keys.

`services/catalog_identity.normalize_gtin` zero-PADS to GTIN-14. So every short
placeholder a supplier drops in a barcode field becomes structurally valid:

    '0'         -> '00000000000000'
    '1'         -> '00000000000001'
    '1234'      -> '00000000001234'
    '123456789' -> '00000123456789'

`intake_identity.canonical_gtin` feeds that to ADR-011 Tier-0 exact matching,
which is GLOBAL by design — a GS1 GTIN identifies one physical product across
every merchant — so it ATTACHES rather than flags. Two merchants whose feeds both
say `1` were measured attaching to each other's identity.

WHY THIS FILE NO LONGER PINS `normalize_gtin`. Its first version added a bespoke
`set(norm) == {"0"}` guard here and then pinned `normalize_gtin` unchanged, on
the reasoning that `make_content_key` folds it and changing it would re-key
existing rows. Review established two things that dismantled that:

  * the repo ALREADY had this rule — `services/strong_identifier
    ._normalize_digit_identifier` carries the identical all-zero idiom PLUS the
    length gate that kills the rest of the family. The bespoke guard was a
    second, weaker copy: exactly the duplicate-rule defect the audit that
    produced this change exists to remove.
  * the population that pin protected is EMPTY. Prod has 0 rows with any GTIN
    and 0 content_keys folding one (`catalog_invariant_checks.py:594` says so
    outright: "not sparse, EMPTY"). The pin protected nothing and was the only
    test blocking the correct fix.

So the guard is now DELEGATION, and these tests assert the behaviour rather than
the mechanism — they pass whether the rule lives in the shared helper or moves
into `normalize_gtin` later.
"""

from __future__ import annotations

import pytest

from services.intake_identity import canonical_gtin
from services.strong_identifier import normalize_strong_identifier


# Every spelling of "nothing" that `normalize_gtin` would pad into a valid
# GTIN-14. The all-zero cases are the original defect; the rest are the family
# the bespoke guard let through.
@pytest.mark.parametrize("raw", [
    "0", "00", "000", "00000000000000", " 0 ",     # all-zero
    "1", "9", "01", "11", "111", "1234", "12345",  # short placeholders
    "123456789",                                   # short, padded by normalize_gtin
    "n/a", "N/A", "none", "unknown", "-", "null",  # textual placeholders
])
def test_placeholders_never_become_match_keys(raw):
    assert canonical_gtin(raw) is None, (
        f"canonical_gtin({raw!r}) returned a match key. Tier-0 GTIN matching is "
        "global across merchants and ATTACHES, so this joins unrelated products.")


@pytest.mark.parametrize("raw,expected", [
    ("8809416470108", "08809416470108"),   # 13-digit, padded to 14
    ("08809416470108", "08809416470108"),  # already 14
    ("00000000000017", "00000000000017"),  # contains zeros but is not all-zero
    ("  8809416470108  ", "08809416470108"),
])
def test_real_gtins_survive(raw, expected):
    """The guard must not be a blanket reject."""
    assert canonical_gtin(raw) == expected


def test_the_rule_is_delegated_not_reimplemented():
    """The point of the rewrite: ONE implementation, not two.

    Asserted as agreement rather than by reading the source — if `canonical_gtin`
    ever grows its own copy again, it will disagree with the shared helper on
    some input in the table above and one of these tests fails.
    """
    for raw in ("0", "1", "1234", "n/a", "8809416470108", "00000000000017"):
        shared = normalize_strong_identifier(raw, "gtin")
        mine = canonical_gtin(raw)
        assert (shared is None) == (mine is None), (
            f"canonical_gtin and strong_identifier disagree on {raw!r} — "
            "the rule has been re-implemented instead of delegated.")


def test_a_valid_LENGTH_bogus_gtin_is_NOT_caught_and_that_is_a_known_gap():
    """Honest boundary: '0000000000001' is 13 digits — a legitimate GTIN-13
    LENGTH — and not all-zero, so neither the length gate nor the all-zero
    reject fires. It survives as '00000000000001'.

    Nothing in this change claims to catch it, and the length gate cannot
    without rejecting real GTINs. What WOULD catch it is GS1 check-digit
    validation: the check digit for twelve zeros is 0, not 1, so this value is
    arithmetically invalid.

    Deliberately NOT added here. Check-digit validation is a separate decision
    with its own blast radius — it would reject every malformed-but-currently-
    accepted barcode in the corpus at once, which needs measuring first. This
    test exists so the gap is recorded rather than mistaken for coverage.
    """
    assert canonical_gtin("0000000000001") == "00000000000001"


def test_empty_and_none_stay_none():
    assert canonical_gtin(None) is None
    assert canonical_gtin("") is None


def test_every_deposit_basis_call_passes_the_GUARDED_gtin():
    """The guard is useless if the deposit gate is handed the raw value.

    `canonical_gtin` was guarded while `_deposit_basis(brand, title, gtin, ...)`
    still received the UNGUARDED parameter, so one call to
    `resolve_or_attach_content_identity` reported `gtin=None` AND
    `deposit_basis='gtin'` with `confidence=1.0`, `is_depositable=True` — the
    basis whose own docstring reads "a real GTIN was folded into the key".
    A placeholder barcode opened the P0.2 deposit gate at maximum confidence.

    Checked by AST, not by grep: a text search is defeated by a comment
    mentioning the old form or by reformatting, and this file's whole subject is
    guards that look right and are not.
    """
    import ast
    import pathlib

    src = (pathlib.Path(__file__).resolve().parent.parent
           / "services" / "intake_identity.py").read_text()
    offenders = []
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        if not (isinstance(fn, ast.Name) and fn.id == "_deposit_basis"):
            continue
        if len(node.args) < 3:
            continue
        third = node.args[2]
        name = third.id if isinstance(third, ast.Name) else ast.dump(third)
        if name != "gtin14":
            offenders.append(f"line {node.lineno}: _deposit_basis(..., {name}, ...)")

    assert not offenders, (
        "deposit basis is being derived from an UNGUARDED gtin:\n  "
        + "\n  ".join(offenders)
        + "\nPass `gtin14` (the canonical_gtin result), not the raw parameter.")
