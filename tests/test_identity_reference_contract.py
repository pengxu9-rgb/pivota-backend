"""Pin IDENTITY_REFERENCE.md's load-bearing claims about `ext:` to the code.

WHY THIS EXISTS. The `ext:` product_key generation was absent from
IDENTITY_REFERENCE §2 entirely, and §4 described those rows only as
"720 other/bare". "Other" reads as unclassified residue. A backfill acted on that
reading, rewrote 720 seeds from `ext:` to `prod::`, and 364 elected public PDPs
returned HTTP 500 (2026-08-01).

The doc now says the opposite, correctly. But a doc is exactly the defense that
has already failed this codebase repeatedly — prose plus reviewer memory does not
scale, which is the same finding that produced `identity_join_sql`'s lint. So the
claims that would cause harm if they silently went stale are asserted here
against the real modules. If someone retires the `ext:` lane for real, these
tests fail and the doc gets updated in the same change.
"""

from __future__ import annotations

import pathlib

_DOC = pathlib.Path(__file__).resolve().parent.parent / "docs" / "IDENTITY_REFERENCE.md"


def test_ext_keys_are_merchant_agnostic():
    """THE structural claim. `prod::` is per-merchant; `ext:` is derived from
    (brand, product_name) alone, so it is content-grained. If this ever becomes
    merchant-scoped, the ladder in §1 and Trap T8 are both wrong."""
    from services.catalog_enrichment_agent.ingestion import derive_product_key

    key = derive_product_key("COSRX", "Snail Mucin Essence")
    assert key.startswith("ext:"), key
    assert "::" in key, key
    # Same product, no merchant anywhere in the input or the output.
    assert derive_product_key("COSRX", "Snail Mucin Essence") == key
    # A different product must not collide.
    assert derive_product_key("COSRX", "Advanced Snail 96") != key


def test_the_ext_lane_is_still_live():
    """The doc says new `ext:` rows are still minted. If Path-C stops minting
    them, "frozen legacy" becomes true in practice and the doc must say so."""
    from services.catalog_enrichment_agent import ingestion

    source = pathlib.Path(ingestion.__file__).read_text()
    assert "derive_product_key(" in source
    # Called, not merely defined.
    calls = [ln for ln in source.split("\n")
             if "derive_product_key(" in ln and not ln.strip().startswith("def ")]
    assert calls, "derive_product_key is defined but never called"


def test_live_code_treats_ext_as_canonical_not_as_residue():
    """`pdp_identity_recovery` PREFERS an `ext:` product_key and labels it
    canonical. That is the strongest evidence these rows are not legacy."""
    recovery = (pathlib.Path(__file__).resolve().parent.parent
                / "services" / "pdp_identity_recovery.py").read_text()
    assert "canonical_ext_product_key" in recovery
    assert "cp.product_key LIKE 'ext:%'" in recovery
    assert "pg_ext_" in recovery


def _section(doc: str, heading: str) -> str:
    """The body of one `###` section, up to the next one.

    Asserting against the WHOLE doc is not enough: §1's pointer and Trap T8 both
    mention `ext:`, so deleting the entire §2 section left every assertion here
    green. Measured — the first version of this test survived exactly that
    deletion.
    """
    start = doc.find(heading)
    assert start != -1, f"missing section: {heading}"
    nxt = doc.find("\n### ", start + len(heading))
    return doc[start: nxt if nxt != -1 else len(doc)]


def test_the_reference_documents_the_ext_generation():
    """The section whose ABSENCE caused the incident."""
    doc = _DOC.read_text()
    # The trap ENTRY, not the string: 'T8' also appears in §1's pointer, so a
    # bare substring check survived truncating the trap index. Measured.
    assert "- **T8 —" in doc, "the trap index must carry the two-generations trap"

    body = _section(doc, "### `product_key` — the `ext:` generation")
    assert "derive_product_key" in body, "the section must name the minter"
    assert "merchant-agnostic" in body.lower(), "the grain claim must be in the section"
    assert "catalog_enrichment_agent_v1" in body, "the section must name the lane"
    assert "pg_ext_" in body
    assert "canonical_ext_product_key" in body
    assert "ADR-011" in body, "the section must place the 'frozen legacy' claim"

    # §4 must NAME the format rather than calling it "other". Assert the
    # positive claim, not the absence of the old phrasing: the doc quotes that
    # phrasing verbatim while explaining why it was wrong, so an
    # absence-assertion here fails on the very text that fixes it.
    assert "720 `ext:`" in doc, (
        "the attached_product_key section must name the ext: format — calling a "
        "live generation 'other/bare' is what caused the 2026-08-01 outage")


def test_the_reference_warns_against_converting_between_generations():
    """The specific action that took 364 PDPs down."""
    doc = _DOC.read_text()
    lowered = doc.lower()
    assert "attached_product_key" in doc
    assert "500" in doc, "the doc should carry the measured cost"
    assert "never convert" in lowered or "must not" in lowered
