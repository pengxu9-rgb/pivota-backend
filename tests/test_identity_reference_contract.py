"""Pin IDENTITY_REFERENCE.md's load-bearing claims about `ext:` to the code.

WHY THIS EXISTS. The `ext:` product_key generation was absent from
IDENTITY_REFERENCE §2 entirely, and §4 described those rows only as
"720 other/bare". "Other" reads as unclassified residue. A backfill acted on that
reading, rewrote 720 seeds from `ext:` to `prod::`, and 364 elected public PDPs
returned HTTP 500 (2026-08-01).

WHAT A TEST OVER A MARKDOWN FILE CAN AND CANNOT DO. It cannot validate MEANING.
The first version of this file was keyword-presence checks, and review showed it
stayed green while §2 was rewritten to say the exact opposite — that `ext:` is
retired, is NOT merchant-agnostic, and that "it is SAFE to re-key `ext:` rows to
`prod::` in bulk". Every required keyword was still present; `merchant-agnostic`
even matched inside "NOT merchant-agnostic". A green test asserting the doc is
right, while the doc licenses the next outage, is worse than no test.

So the weight is split deliberately:

  * the CODE claims are proved against the code, structurally — the derivation is
    recomputed, the mint path is executed, sources are read with comments
    stripped (a comment carrying the greped string defeated three of these);
  * the DOC gets a narrow pin on the exact SENTENCES whose deletion or inversion
    was demonstrated in review. That is honest about its reach: a determined
    rewrite that keeps those sentences and adds contradictory prose alongside
    them would still pass, and no string test can fix that. What it does
    guarantee is that the specific regressions which actually happened cannot
    recur silently.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import pathlib
import re

_REPO = pathlib.Path(__file__).resolve().parent.parent
_DOC = _REPO / "docs" / "IDENTITY_REFERENCE.md"


def _code_only(path: pathlib.Path) -> str:
    """Source with comments and docstrings blanked.

    Three tests here were defeated by prepending a COMMENT that carried the
    string being greped for, while the real code changed underneath. Read code,
    not prose.
    """
    src = path.read_text()
    lines = src.split("\n")

    def blank(l1, c1, l2, c2):
        for ln in range(l1, l2 + 1):
            i = ln - 1
            if not (0 <= i < len(lines)):
                continue
            a = c1 if ln == l1 else 0
            b = min(c2 if ln == l2 else len(lines[i]), len(lines[i]))
            if b > a:
                lines[i] = lines[i][:a] + " " * (b - a) + lines[i][b:]

    try:
        tree = ast.parse(src)
    except SyntaxError:
        tree = None
    if tree is not None:
        holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        for node in ast.walk(tree):
            if isinstance(node, holders):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and \
                        isinstance(body[0].value, ast.Constant) and \
                        isinstance(body[0].value.value, str):
                    d = body[0]
                    blank(d.lineno, d.col_offset, d.end_lineno, d.end_col_offset)
    return "\n".join(re.sub(r"#.*$", "", ln) for ln in lines)


# ---------------------------------------------------------------------------
# THE CODE CLAIMS — proved, not greped
# ---------------------------------------------------------------------------


def test_ext_keys_are_derived_from_brand_and_product_name_alone():
    """THE structural claim: `ext:` is MERCHANT-AGNOSTIC, so it sits at content
    grain while `prod::` sits at listing grain (§1, Trap T8).

    The first version asserted only format and determinism — every one of which
    a merchant-SCOPED key also satisfies. Adding a merchant to the digest left it
    green, which would have falsified the doc's central claim undetected.

    Two independent proofs instead:
      1. the signature takes no merchant, so it CANNOT vary by one;
      2. the output is recomputed from (brand, product_name) alone, so any salt
         — merchant or otherwise — breaks this.
    """
    from services.catalog_enrichment_agent.ingestion import (
        canonical_product_name,
        derive_product_key,
    )

    assert list(inspect.signature(derive_product_key).parameters) == [
        "brand", "product_name",
    ], "derive_product_key must take no merchant — that is what merchant-agnostic MEANS"

    canonical = canonical_product_name("COSRX", "Snail Mucin Essence")
    expected = f"ext:{canonical[:200]}::{hashlib.sha1(canonical.encode()).hexdigest()[:8]}"
    assert derive_product_key("COSRX", "Snail Mucin Essence") == expected, (
        "the derivation changed — if a merchant (or anything else) now salts this "
        "key, `ext:` is no longer content-grained and §1/T8 are wrong")


def test_the_live_mint_path_still_emits_ext_keys():
    """The doc says Path-C still mints `ext:` today.

    EXECUTE the mint path. The first version greped for a call on a non-`def`
    line, which a comment naming the function — or the call moved inside
    `if False:` — satisfied just as well.
    """
    from services.catalog_enrichment_agent.ingestion import _build_pdp_insert
    from services.seller_identity import (
        BANNED_BUCKET_MERCHANT_ID,
        resolve_seed_seller_identity,
    )

    seller = resolve_seed_seller_identity(brand="COSRX", domain="cosrx.com")
    row = _build_pdp_insert(
        pdp_payload={"brand": "COSRX", "product_name": "Snail Mucin Essence"},
        offers=[],
        source_jsonl=None,
        seller=seller,
    )
    assert row["product_key"].startswith("ext:"), row["product_key"]
    # W2 (2026-08-06) migrated this contract: the merchant column is now the
    # OBSERVED SELLER OF RECORD, never the banned sentinel. The `ext:` key
    # format survives for its own reason — keys are opaque storage tokens
    # (ADR-009 D4.2) and the derivation inputs are deliberately untouched, so
    # the key never bakes ANY merchant in, old or new.
    assert row["merchant_id"] == seller["merchant_id"]
    assert row["merchant_id"] != BANNED_BUCKET_MERCHANT_ID


def test_live_code_treats_ext_as_canonical_not_as_residue():
    """`pdp_identity_recovery` PREFERS an `ext:` product_key and labels it
    canonical — the strongest evidence these rows are not legacy.

    Comments stripped: renaming the real label while leaving a comment carrying
    the old string defeated the first version.
    """
    code = _code_only(_REPO / "services" / "pdp_identity_recovery.py")
    assert "canonical_ext_product_key" in code
    assert "cp.product_key LIKE 'ext:%'" in code
    assert "pg_ext_" in code


# ---------------------------------------------------------------------------
# THE DOC — narrow pins on what was actually lost
# ---------------------------------------------------------------------------

# The load-bearing SENTENCES, pinned verbatim.
#
# NOT a blacklist of dangerous phrases. That was the first attempt and it failed
# immediately: it matched the doc's own correct sentence "…are retired,
# detachable, or safe to re-key" — a NEGATED usage. Prose cannot be validated by
# forbidden-keyword matching, because negation exists and the doc must be free to
# quote the wrong idea in order to reject it.
#
# Pinning the assertions themselves is sound in the direction that matters:
# rewriting the section to claim the OPPOSITE requires deleting these sentences,
# and their deletion is exactly what this catches. It does not stop someone
# adding contradictory prose alongside them — no string test can. The code tests
# above are what actually prove the claims.
_PINNED_SENTENCES = (
    # The grain claim (§2). Its inverse cannot coexist with it.
    "derived from **(brand, product_name) ONLY** — it is **merchant-agnostic**",
    # The prohibition whose DELETION review demonstrated, keeping all keywords.
    '- **🚨 MUST NOT:** "repair" an `ext:` key to a `prod::` key.',
)


def _sections(doc: str) -> dict:
    """`###` sections, ignoring headings inside fenced code blocks.

    Sub-headings INSIDE an identifier section must use `####`, not `###` — this
    file uses `###` for each identifier, so a `###` genuinely starts a new one.
    Verified: a `#### Minting details` inside §2 does not break these tests; a
    `### ` one does, and should, because it means the section actually ended.
    """
    out, current, buf, fenced = {}, None, [], False
    for line in doc.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and line.startswith("### "):
            if current is not None:
                out[current] = "\n".join(buf)
            current, buf = line[4:].strip(), [line]
            continue
        buf.append(line)
    if current is not None:
        out[current] = "\n".join(buf)
    return out


def test_the_reference_documents_the_ext_generation():
    """The section whose ABSENCE caused the incident."""
    doc = _DOC.read_text()
    # The trap ENTRY, not the string: 'T8' also appears in §1's pointer, so a
    # bare substring check survived truncating the trap index. Measured.
    assert "- **T8 —" in doc, "the trap index must carry the two-generations trap"

    matches = [k for k in _sections(doc) if "the `ext:` generation" in k]
    assert matches, "§2 has no `ext:` generation section"
    body = _sections(doc)[matches[0]]

    assert "derive_product_key" in body, "the section must name the minter"
    assert "merchant-agnostic" in body.lower(), "the grain claim must be in the section"
    assert "catalog_enrichment_agent_v1" in body, "the section must name the lane"
    assert "pg_ext_" in body
    assert "canonical_ext_product_key" in body

    # §4 must NAME the format rather than calling it "other". Assert the positive
    # claim, not the absence of the old phrasing: the doc quotes that phrasing
    # verbatim while explaining why it was wrong.
    assert "720 `ext:`" in doc, (
        "the attached_product_key section must name the ext: format — calling a "
        "live generation 'other/bare' is what caused the 2026-08-01 outage")


def test_the_reference_forbids_converting_between_generations():
    """The specific instruction that took 364 PDPs down must be present as a
    PROHIBITION, and its inverse must never appear as guidance.

    Deleting the 🚨 bullet entirely left the first version of this test green.
    """
    doc = _DOC.read_text()
    lowered = doc.lower()

    for sentence in _PINNED_SENTENCES:
        assert sentence in doc, (
            f"a load-bearing sentence was removed or reworded:\n  {sentence}\n"
            "Rewriting this section to the opposite claim requires deleting it. "
            "If the change is deliberate, update this test in the same commit "
            "and say why.")
    assert "364" in doc, "the doc must carry the measured cost of doing it"
    assert "never convert" in lowered or "must not" in lowered
