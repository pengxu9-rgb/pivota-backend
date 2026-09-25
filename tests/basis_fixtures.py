"""Measurement-basis fixtures built BY THE WRITER, never hand-typed.

WHY. The recovery contract refuses a numerical before/after comparison unless
BOTH runs carry a complete, matching measurement basis
(``db.audit_basis.bases_are_comparable``). A test that hands
``build_reaudit_delta`` two reports and no basis therefore no longer exercises a
comparison at all — it exercises the refusal, and asserting ``same is True`` on
it can only be made to pass by weakening the contract.

The repair is the FIXTURE, and it must come from the writer rather than from a
literal. The basis is not a shape a test may invent: which fields exist, how
they are normalised (``official_domains`` is stored ``sorted({lower(strip)})``)
and what ``methodology_version`` currently is all live in one place, and a
hand-copied dict rots the moment any of them moves. That is exactly how a
literal ``"methodology_version": "1"`` in
``tests/services/test_report_assembly_harness.py`` stopped matching the current
run's in-memory basis the instant ``METHODOLOGY_VERSION`` went to ``"2"``.

So these helpers call the real producers:

* :func:`services.coverage_profiles.resolve_provider_models` for the report's
  own ``provider_models`` block — the block ``build_providers_and_models``
  reads, and the one a real assembled report carries; and
* :func:`services.audit_evidence_builder.record_audit_basis` with
  ``persist=False`` — literally the call
  ``agent_center_bd_report_service._basis_pair_for_delta`` makes to build the
  CURRENT run's basis — for the basis payload itself.

A fixture built this way stays correct across a methodology bump, and a mutant
in the comparison path breaks it.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

# The merchant a basis fixture is built under. `record_audit_basis` snapshots
# this merchant's official-domain set, so BOTH sides of a comparison must be
# built under the same id or `official_domains` diverges for a reason the test
# never intended.
BASIS_MERCHANT_ID = "m-basis"


def provider_models_block(
    providers: Sequence[str] = ("gemini",),
) -> Dict[str, Dict[str, Any]]:
    """The ``provider_models`` block a real assembled report carries.

    ``providers_and_models`` is an EVIDENCE-REQUIRED comparability component
    (``db.audit_basis._EVIDENCE_REQUIRED_FIELDS``): two bases that are both
    missing it are not comparable, however equal they look. A report fixture
    that omits this block therefore cannot support any comparison, so every
    fixture that needs one gets it from the resolver that writes it in
    production rather than from a literal model name.
    """
    from services.coverage_profiles import resolve_provider_models

    block = resolve_provider_models(list(providers))
    assert block, (
        "resolve_provider_models returned nothing for "
        f"{providers!r}; a basis built from this report would be "
        "non-comparable for a reason the test does not intend"
    )
    return block


def with_provider_models(
    report: Dict[str, Any], providers: Sequence[str] = ("gemini",)
) -> Dict[str, Any]:
    """Stamp :func:`provider_models_block` onto a report fixture, in place."""
    report["provider_models"] = provider_models_block(providers)
    return report


async def writer_basis(
    brand_report: Mapping[str, Any],
    *,
    merchant_id: str = BASIS_MERCHANT_ID,
) -> Dict[str, Any]:
    """The basis payload the writer builds for ``brand_report``.

    Same call, same kwargs, as the production comparability path
    (``_basis_pair_for_delta`` → ``record_audit_basis(audit_run_id="",
    persist=False)``), so what a test compares is what a run compares.
    """
    from services.audit_evidence_builder import record_audit_basis

    payload = await record_audit_basis(
        audit_run_id="",
        brand_report=brand_report,
        merchant_id=merchant_id,
        persist=False,
    )
    assert payload is not None, (
        "record_audit_basis(persist=False) returned None — the build-only path "
        "is the one the comparability check depends on; a fixture cannot paper "
        "over it"
    )
    return payload


async def comparable_writer_bases(
    current_report: Mapping[str, Any],
    prior_report: Optional[Mapping[str, Any]] = None,
    *,
    merchant_id: str = BASIS_MERCHANT_ID,
) -> tuple:
    """``(current_basis, prior_basis)`` for a pair of reports, from the writer.

    ``prior_report`` defaults to ``current_report`` for the common case of a
    test that varies only the SCORES between the two runs and wants the basis
    held genuinely constant — which is what a re-run on a pinned set is.
    """
    current = await writer_basis(current_report, merchant_id=merchant_id)
    prior = await writer_basis(
        prior_report if prior_report is not None else current_report,
        merchant_id=merchant_id,
    )
    return current, prior
