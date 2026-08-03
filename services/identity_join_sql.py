"""Single source of truth for "which `pdp_identity_listing` row belongs to THIS
catalog row?".

WHY THIS MODULE EXISTS. The answer is two conjuncts and a lane test, all of them
non-obvious, and every one has been got wrong at least once by someone reading
the rule and then retyping the SQL:

  * ``pil.merchant_id = cp.merchant_id`` — ``source_listing_ref`` is
    ``merchant_id:product_id`` (ADR-008), so ``product_id`` ALONE is not unique.
    Omit this and the join FANS OUT, injecting a foreign merchant's
    ``sellable_item_group_id`` into whatever aggregate is asking.
  * the LANE TEST — a Path-C (``catalog_enrichment_agent_v1``) row's
    ``source_product_id`` is a name slug, not a seed id, so it must route through
    its attached seed. Omit this and the join silently MISSES for the entire
    minted lane: the group reads NULL, and since ``count(DISTINCT)`` skips NULLs,
    a consumer counting distinct groups cannot see that lane at all.
  * the SEED PICK — Path-C attaches one seed PER OFFER to the same
    ``product_key``, so choosing among them is not a formality.
    ``MINTED_SEED_IDENTITY_LEG`` prefers a seed that CARRIES a listing precisely
    because the winner's ``external_product_id`` IS the join key; a bare
    ``updated_at DESC`` picks an identity-less sibling (real state goes
    uncounted) or a stale inactive seed (clean state counts as broken). Measured
    both directions on 2026-07-31.

`services/catalog_row_trust_upserter._PRODUCT_JOIN_SELECT` expresses the same
decision through its ``minted_seed_one`` CTE, which is the right shape when you
are already scanning a page of rows. This module is the right shape for a
correlated one-row-at-a-time consumer, and `tests/test_identity_join_sql.py`
pins that the two agree on the lane decision rather than letting them drift.

THE POINT IS NOT DE-DUPLICATION. It is that a new consumer should be unable to
hand-roll a subtly-wrong join without deleting an import first. The one defense
in this codebase that has never drifted is the byte-identical SQL pin between the
two repos (``seed_route_resolves_sql``, ``priced_offer_exists_sql``); this is the
same idea applied to the join that identity defects keep landing on.
"""

from __future__ import annotations

from services.source_quarantine import MINTED_SEED_IDENTITY_LEG, SEED_PICK_ORDER

# The source_system that marks a Path-C minted canonical. Mirrors
# PIVOTA-Agent pdpRenderability MINTED_SOURCE_SYSTEM.
MINTED_SOURCE_SYSTEM = "catalog_enrichment_agent_v1"


def minted_seed_external_id_sql(cp_alias: str = "cp") -> str:
    """The `external_product_id` of the seed that represents a minted row.

    Correlated form of `catalog_row_trust_upserter`'s ``minted_seed_one`` CTE.
    Uses the SHARED order constants — never a hand-rolled ``updated_at DESC``.
    """
    return (
        "(SELECT s.external_product_id\n"
        "                   FROM external_product_seeds s\n"
        f"                  WHERE s.attached_product_key = {cp_alias}.product_key\n"
        f"                  ORDER BY {MINTED_SEED_IDENTITY_LEG}, {SEED_PICK_ORDER}\n"
        "                  LIMIT 1)"
    )


def identity_listing_product_id_sql(cp_alias: str = "cp") -> str:
    """The `pdp_identity_listing.product_id` that belongs to this catalog row.

    Mirror rows carry the seed id in ``source_product_id``; minted rows carry a
    name slug and must route through their attached seed.
    """
    return (
        f"CASE WHEN {cp_alias}.source_system = '{MINTED_SOURCE_SYSTEM}'\n"
        f"                      THEN {minted_seed_external_id_sql(cp_alias)}\n"
        f"                      ELSE {cp_alias}.source_product_id\n"
        "                 END"
    )


def identity_listing_lateral_sql(
    cp_alias: str = "cp",
    *,
    columns: str = "sellable_item_group_id",
    alias: str = "pil",
) -> str:
    """`LEFT JOIN LATERAL` yielding at most ONE listing for this catalog row.

    LATERAL + ``LIMIT 1`` rather than a plain join because a fanned-out listing
    corrupts any aggregate over the result. The ``ORDER BY`` is determinism, not
    preference — a ``LIMIT`` without one is plan-dependent.

    ``columns`` is a comma-separated list of BARE column names, qualified here
    with ``alias``. It takes bare names rather than a ready-made select-list
    because the first version took the latter, defaulted it to
    ``"pil.sellable_item_group_id"``, and so emitted
    ``SELECT pil.… FROM pdp_identity_listing lst`` — invalid SQL — for any caller
    that passed ``alias`` without also remembering to restate ``columns``. That
    is the same right-value/wrong-alias shape this module exists to prevent, so
    the coupling is removed rather than documented.

    It is a literal SQL fragment either way, and never a place for user input.
    """
    select_list = ", ".join(
        f"{alias}.{name.strip()}" for name in columns.split(",") if name.strip()
    )
    return (
        "LEFT JOIN LATERAL (\n"
        f"                SELECT {select_list}\n"
        f"                FROM pdp_identity_listing {alias}\n"
        f"                WHERE {alias}.merchant_id = {cp_alias}.merchant_id\n"
        f"                  AND {alias}.product_id = {identity_listing_product_id_sql(cp_alias)}\n"
        f"                ORDER BY {alias}.source_listing_ref\n"
        "                LIMIT 1\n"
        f"            ) {alias} ON TRUE"
    )
