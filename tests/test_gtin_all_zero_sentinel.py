"""An all-zero GTIN is a sentinel, and Tier-0 matching is GLOBAL.

`services/catalog_identity.normalize_gtin` zero-pads to GTIN-14, so a source
field holding '0' — or '', or '000' — normalizes to '00000000000000'. That is
structurally a valid GTIN-14, and `intake_identity` feeds it to Tier-0 exact
matching, which is global BY DESIGN: a GS1 GTIN identifies one physical product
across every merchant.

So a single placeholder zero in one supplier feed would ATTACH unrelated
products to each other, across merchants, using the highest-confidence matcher
in the system — the one whose whole premise is that a GTIN is unique.

THE GUARD IS AT THE CONSUMER, NOT IN THE NORMALIZER, and that placement is
load-bearing: `services/catalog_identity.make_content_key` folds `normalize_gtin`
into the key it mints. Changing the normalizer would silently re-key every row
already minted with a zero GTIN — an identity change, which is exactly the class
of edit that took 364 public PDPs to HTTP 500 on 2026-08-01. The consumer drops
the value; the minted key is untouched.
"""

from __future__ import annotations

import pytest

from services.catalog_identity import normalize_gtin
from services.intake_identity import canonical_gtin


@pytest.mark.parametrize("raw", ["0", "00", "000", "00000000000000", " 0 ", "0000000000000"])
def test_all_zero_inputs_are_rejected_as_identifiers(raw):
    """Every spelling of "nothing" that the normalizer pads into a valid GTIN-14."""
    assert canonical_gtin(raw) is None, (
        f"canonical_gtin({raw!r}) returned a match key. Tier-0 GTIN matching is "
        "global across merchants, so this attaches unrelated products.")


def test_the_normalizer_itself_is_deliberately_unchanged():
    """Pins WHERE the guard lives.

    If someone 'fixes' this in `normalize_gtin` instead, `make_content_key`
    folds that output and every row already keyed with a zero GTIN silently
    re-keys. This test fails if the guard migrates there, so the move has to be
    a deliberate decision with a backfill, not a tidy-up.
    """
    assert normalize_gtin("0") == "00000000000000", (
        "normalize_gtin changed. It is folded into make_content_key — moving the "
        "all-zero guard here re-keys existing rows. See the module docstring.")


def test_a_real_gtin_still_survives():
    """The guard must not be a blanket reject."""
    real = canonical_gtin("8809416470108")
    assert real is not None and len(real) == 14 and set(real) != {"0"}


def test_a_gtin_that_merely_contains_zeros_is_kept():
    """Only ALL zeros is the sentinel — leading/embedded zeros are ordinary."""
    assert canonical_gtin("00000000000017") == "00000000000017"


def test_empty_and_none_stay_none():
    assert canonical_gtin(None) is None
    assert canonical_gtin("") is None
