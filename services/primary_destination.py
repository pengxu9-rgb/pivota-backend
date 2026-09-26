"""B3 — the PRIMARY COMMERCE DESTINATION of one grounded AI answer.

An AI shopping answer cites many hosts. The report already says WHO was cited
(``authority_map``) and what each host is RELATIVE TO THE MERCHANT
(``citation_role``, services/cited_host_classifier.py). Neither answers the
question a merchant actually asks: **where did the answer send the buyer?**

A citation list is not a destination list. Most cited hosts are SOURCES — the
editorial round-up the model read, the forum thread it paraphrased, the CDN the
image came from. A shopper cannot buy from any of them. The destination is the
subset the shopper could actually transact on: the merchant's own storefront, a
retailer or marketplace, or another brand's store. This module picks AT MOST ONE
of those per response and calls it the primary destination.

WHY "AT MOST ONE". The merchant-facing claim this feeds is singular by
construction — "for this question, AI sends buyers to X" — so a response that
names three retailers must resolve to one, deterministically, or the number is
not comparable between runs. And a response that names NONE must resolve to
nothing at all: see below.

WHAT THIS MODULE DELIBERATELY DOES NOT USE
------------------------------------------
The original spec for this signal (§12) asked for two more inputs: explicit
buy/purchase language ("you can order it from…") and the surrounding answer
context around each citation. **Neither is available, and neither is
approximated here.** The audit persists only ``grounding_sources`` (uri +
title) and a 280-character ``evidence_excerpt`` per run — the full answer text
is never stored. A buy-language signal derived from a 280-character excerpt
would be a different measurement wearing the same name, so it is left out and
named here instead of being faked. If the answer text is ever persisted, that
is a PRIMARY_DESTINATION_VERSION bump, and the version is exactly what makes
the change visible to a before/after diff (see db/audit_basis.py).

THE INPUT THAT IS AVAILABLE, AND WHY IT CARRIES THE SIGNAL
----------------------------------------------------------
Citation ORDER. Grounding sources arrive in the order the model attached them
to its answer, and that order survives into the report — but ONLY at
``build_authority_map``'s source loop. Everything downstream
(``sku["authority_hosts"]``) is a HOST-keyed aggregate, so ordering is already
lost by the time services/audit_evidence_builder.py sees it. The ordinal must
therefore be captured at that loop and threaded down; this module consumes it.

Ordinal is a weak signal on its own — but it is a REAL one, it is stable across
re-runs of the same answer, and it is the only ordering evidence the pipeline
retains. Ranking commerce hosts by it is honest; inventing a purchase-intent
score for them would not be.

NO DESTINATION IS A RESULT, NOT A FAILURE
-----------------------------------------
``select_primary_destination`` returns None whenever a response cited no
plausible commerce host. That is the "AI answered your category question and
gave the shopper nowhere to buy" case — an actionable finding in its own right,
and the reason the caller must distinguish None from an error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Union

# Bump when the SELECTION RULE changes in any way that could move which host is
# named primary — the admission set, the ordering, or the tie-break. Recorded on
# every run in audit_basis.primary_destination_version, and compared by
# db.audit_basis.bases_are_comparable, so a rule change can never be read as
# merchant movement.
PRIMARY_DESTINATION_VERSION = 1

# The FOLDED authority host type (services/agent_center_bd_report_service.
# _classify_authority_host) that denotes a place a shopper can transact.
# `_classify_authority_host` already folds classify_host's `retailer`,
# `marketplace` and `brand` — a retailer, a marketplace, and another brand's
# storefront — into this single value, which is precisely the spec's commerce
# set. Naming it as a frozenset of one keeps the admission rule greppable and
# makes widening it a deliberate edit rather than a typo.
COMMERCE_HOST_TYPES = frozenset({"retailer"})

# Named for the reader, and asserted against in tests: these are SOURCES, not
# destinations. An answer citing only these sent the buyer nowhere. `trade` is
# the trade-press fold of `editorial`; `reddit` is the dedicated fold of the
# forum family; `unclassified` is a host the cited-host registry has never seen
# and therefore CANNOT be asserted to be a store — admitting it would be a guess
# with a merchant-facing consequence.
NON_DESTINATION_HOST_TYPES = frozenset({
    "editorial", "trade", "creator", "forum", "reddit", "unclassified",
})


@dataclass(frozen=True)
class DestinationCandidate:
    """One cited host of ONE response, with its position in that response.

    ``ordinal`` is the zero-based index of the grounding source within the
    run's ``grounding_sources`` list — the model's own citation order, captured
    at the only place it still exists.
    """

    host: str
    ordinal: int
    host_type: Optional[str] = None
    first_party: bool = False


_CandidateLike = Union[DestinationCandidate, Mapping[str, Any]]


def is_commerce_destination(
    host_type: Optional[str],
    first_party: bool = False,
) -> bool:
    """Could a shopper have bought something at this cited host?

    Two admitting facts, both asserted rather than inferred:

    * the merchant's OWN domain (``first_party``) — their storefront is a
      destination whether or not the cited-host registry has ever heard of it,
      which matters because a small brand's own domain is usually unclassified;
    * a folded host type in :data:`COMMERCE_HOST_TYPES` — a retailer, a
      marketplace, or another brand's storefront.

    Everything else answers False, INCLUDING ``unclassified``. That is the
    conservative direction: a host we cannot classify is not evidence of a
    place to buy, and admitting it would let an unrecognised blog become "where
    AI sends your buyers".
    """
    if first_party:
        return True
    return str(host_type or "").strip().lower() in COMMERCE_HOST_TYPES


def _as_candidate(value: _CandidateLike) -> Optional[DestinationCandidate]:
    if isinstance(value, DestinationCandidate):
        return value if value.host else None
    if not isinstance(value, Mapping):
        return None
    host = str(value.get("host") or "").strip().lower()
    if not host:
        return None
    raw_ordinal = value.get("ordinal")
    if raw_ordinal is None:
        return None
    try:
        ordinal = int(raw_ordinal)
    except (TypeError, ValueError):
        return None
    if ordinal < 0:
        return None
    return DestinationCandidate(
        host=host,
        ordinal=ordinal,
        host_type=value.get("host_type"),
        first_party=bool(value.get("first_party")),
    )


def commerce_candidates(
    candidates: Iterable[_CandidateLike],
) -> List[DestinationCandidate]:
    """The admitted subset, in selection order (best first).

    Exposed separately from :func:`select_primary_destination` so a caller (and
    a test) can see WHICH hosts were considered, not only which one won.
    """
    admitted = [
        c
        for c in (_as_candidate(v) for v in candidates or ())
        if c is not None and is_commerce_destination(c.host_type, c.first_party)
    ]
    return sorted(admitted, key=_selection_key)


def _selection_key(candidate: DestinationCandidate) -> Sequence[Any]:
    """The total order over admitted candidates. Fully documented because a
    tie-break that is not documented is a tie-break that silently changes.

    1. ``ordinal`` ASC — the model's own citation order is the whole signal.
    2. first-party BEFORE third-party — when the answer attached the merchant's
       own store and a retailer at the SAME position, the merchant's store is
       the destination. This direction is deliberate: the alternative would let
       an arbitrary alphabetical accident decide whether a run scores as
       "AI sent buyers to you" or "AI sent buyers to a retailer".
    3. ``host`` lexicographic — a stable, data-independent final tie-break, so
       the same response always resolves to the same host across processes.
    """
    return (candidate.ordinal, 0 if candidate.first_party else 1, candidate.host)


def select_primary_destination(
    candidates: Iterable[_CandidateLike],
) -> Optional[DestinationCandidate]:
    """The one commerce destination of a single response, or None.

    None means the response cited no plausible commerce host — the
    "no actionable destination" outcome. It is a measurement, not an error, and
    callers must not coerce it into one.
    """
    admitted = commerce_candidates(candidates)
    return admitted[0] if admitted else None


__all__ = (
    "COMMERCE_HOST_TYPES",
    "DestinationCandidate",
    "NON_DESTINATION_HOST_TYPES",
    "PRIMARY_DESTINATION_VERSION",
    "commerce_candidates",
    "is_commerce_destination",
    "select_primary_destination",
)
