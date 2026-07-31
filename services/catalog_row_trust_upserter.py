"""Dual-write helper for ``catalog_row_trust`` (Layer C1 phase 2).

Single concern: take a ``product_key`` (or a predicate), assemble the same
input shape that :mod:`services.catalog_trust_policy.derive_trust` expects,
and UPSERT the resulting row. Fire-and-forget — exceptions are logged and
swallowed so a trust-upsert failure NEVER bubbles up to break a producer.

The Python upserter and the Node ``catalogRowTrustUpserter`` use the same
SQL join shape as ``scripts/backfill-catalog-row-trust.cjs`` (PIVOTA-Agent
#1551) so steady-state output matches periodic backfill output.

Usage from producers::

    from db.database import database
    from services.catalog_row_trust_upserter import upsert_catalog_row_trust

    # after a catalog_products upsert / tombstone write:
    await upsert_catalog_row_trust(db=database, product_key=product_key)

    # quarantine create/revoke — affects every catalog row matching the
    # quarantine predicate:
    await upsert_catalog_row_trust_for_quarantine_match(
        db=database, match_type='domain', match_value='example.myshopify.com',
    )

This module is intentionally I/O-aware; the policy itself
(``services.catalog_trust_policy``) is pure and unit-tested for parity with
the Node side.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from services.catalog_trust_policy import derive_trust
from services.source_quarantine import (
    MINTED_SEED_IDENTITY_LEG,
    SEED_PICK_ORDER,
    STORE_PICK_ORDER,
    sql_bare_domain,
)
from services.pdp_renderability import (
    pdp_route_resolvable,
    seed_route_resolves_sql,
)
from services.priced_offer_sql import priced_offer_exists_sql


logger = logging.getLogger(__name__)


# Cap on how many catalog rows we'll bulk-upsert in one quarantine event.
# A run-away predicate (e.g. a wildcard match) should not stall ingest.
_QUARANTINE_BULK_MAX = 5_000


# ---------------------------------------------------------------------------
# SQL — kept literal & in sync with scripts/backfill-catalog-row-trust.cjs.
# Any change here must land in the Node backfill in the same PR set or the
# steady-state and backfill outputs diverge.
# ---------------------------------------------------------------------------


_PRODUCT_JOIN_CTES = f"""
  WITH external_seed_one AS (
    SELECT DISTINCT ON (s.external_product_id)
      s.id, s.external_product_id, s.status, s.domain, s.attached_product_key, s.updated_at, s.seed_kind
    FROM external_product_seeds s
    -- Tie-break legs come from services.source_quarantine.SEED_PICK_ORDER, the
    -- SAME constant the lateral chain uses. They diverged once (#1643 review:
    -- 6 of 11 shapes resolved different domains, opposite quarantine verdicts
    -- in both directions); defining them in one place is what stops that.
    ORDER BY
      s.external_product_id,
      {SEED_PICK_ORDER}
  ),
  minted_seed_one AS (
    -- Path-C minted canonicals (catalog_enrichment_agent_v1) attach one seed
    -- PER OFFER to the same product_key, so their seed join must be keyed by
    -- attached_product_key and deduped here or the product join would fan out
    -- per offer. Preference order: a seed that CARRIES an identity listing
    -- outranks a merely-newer sibling — the winner's external_product_id is
    -- the pil join key below, so picking an identity-less sibling would
    -- re-derive IDENTITY_CONFIDENCE_NULL nondeterministically as updated_at
    -- values move.
    SELECT DISTINCT ON (s.attached_product_key)
      s.id, s.external_product_id, s.status, s.domain, s.attached_product_key, s.updated_at, s.seed_kind
    FROM external_product_seeds s
    LEFT JOIN pdp_identity_listing spl
      ON spl.product_id = s.external_product_id
    WHERE s.attached_product_key IS NOT NULL
    ORDER BY
      s.attached_product_key,
      {MINTED_SEED_IDENTITY_LEG},
      {SEED_PICK_ORDER}
  ),
  merchant_store_one AS (
    SELECT DISTINCT ON (m.merchant_id, m.platform)
      m.merchant_id, m.platform, m.domain, m.status, m.last_sync
    FROM merchant_stores m
    ORDER BY
      m.merchant_id,
      m.platform,
      {STORE_PICK_ORDER}
  ),
  identity_override_one AS (
    SELECT DISTINCT ON (source_listing_ref)
      id, source_listing_ref, action_type, active
    FROM pdp_identity_override
    WHERE active = TRUE
    ORDER BY
      source_listing_ref,
      updated_at DESC NULLS LAST,
      created_at DESC NULLS LAST,
      id DESC
  )
"""


_PRODUCT_JOIN_SELECT = """
  SELECT
    cp.product_key,
    cp.content_key,
    cp.merchant_id,
    cp.platform,
    cp.source_system,
    cp.source_ref,
    cp.source_product_id,
    cp.source_domain,
    cp.sync_status,
    cp.suppression_reason,
    cp.last_seen_in_sync_at,

    ips.serving_eligible,
    ips.pipeline_stage,
    ips.blocker_code,
    ips.content_quality_score,
    ips.quality_scored_at,
    ips.last_extracted_at,

    pil.source_listing_ref AS pil_source_listing_ref,
    pil.identity_status,
    pil.identity_confidence,
    pil.live_read_enabled,
    pil.review_required,
    pil.sellable_item_group_id,
    pil.product_line_id,
    pil.review_family_id,

    -- c1.v0.5 renderability input. NOT the same question as the eps join
    -- above: eps is restricted to source_system='external_product_seeds_mirror_v1',
    -- while the gateway routes through seeds on TWO keys and this answers for
    -- both. Path-C minted rows (catalog_enrichment_agent_v1) join their seed by
    -- attached_product_key, exactly like the minted_seed_one CTE above; since
    -- P3 (2026-07-25) the gateway resolves that key too, so they answer TRUE
    -- here whenever an acceptable attached seed exists and their PDPs render.
    -- They answered FALSE — and 500ed — until P3 shipped.
    """ + seed_route_resolves_sql("cp") + """ AS pdp_seed_route_ok,

    -- PER-ROW price input for the OFFER_PRICE_MISSING gate. This is the one
    -- serving signal that CANNOT be taken from the ips join below: that join is
    -- `ips.content_key = cp.content_key`, and index_pipeline_state stores ONE
    -- state per content_key (its primary key, migration 098) chosen from the
    -- best of that key's catalog_products rows. Every product_key mints its own
    -- pivota_signature_id and therefore its own public PDP, so reading price
    -- eligibility off the content-grained row publishes a price-less PDP
    -- whenever a priced SIBLING carries the content_key's state.
    --
    -- That is not hypothetical: it put 4 Tom Ford fragrance PDPs
    -- (sig_a2813185…, sig_43e4fca6…, sig_438b3df6…, sig_ffe80d18…) on the
    -- public surface with no price on 2026-07-31, each sharing its content_key
    -- with a priced tomfordbeauty.com row while its own single offer had
    -- list_price, merchant_effective_price and estimated_best_price ALL NULL
    -- and no suppression. `has_price` was never wrong — it was answering about
    -- a different row.
    """ + priced_offer_exists_sql("cp.product_key") + """ AS row_has_priced_offer,

    -- c1.v0.7 canonical-election input. THE grain bridge, and the reason the
    -- OFFER_PRICE_MISSING gate above is a backstop rather than the fix.
    --
    -- content_canonical_election (mig 181) elects ONE pivota_signature_id per
    -- content_key: the single URL the sitemap advertises and that every sibling
    -- sig's PDP names in <link rel="canonical">. index_pipeline_state answers
    -- for the CONTENT, catalog_row_trust answers for the ROW, and this is the
    -- fact that connects them — without it, a non-elected sibling inherits the
    -- content-grained verdict and gets promoted as though it were the canonical.
    --
    -- Measured on prod 2026-07-31: 121 of 6,814 trust-public rows are NOT their
    -- content_key's elected canonical, every one of them on a multi-row
    -- content_key. Those are duplicate PDPs being independently promoted while
    -- their own rel=canonical points at a different page. The 4 Tom Ford rows
    -- were 4 of them — the election had picked the priced tomfordbeauty.com row
    -- correctly in all 4 cases, months before anyone looked at the price.
    --
    -- TRI-STATE and it must stay that way: TRUE/FALSE when an election exists,
    -- NULL when it does not (no row, or the catalog row has no signature yet).
    -- 32 multi-row content_keys still have no election; they must be left alone,
    -- not demoted, until the elector covers them.
    (
        SELECT (cce.canonical_sig_id = cp.pivota_signature_id)
        FROM content_canonical_election cce
        WHERE cce.content_key = cp.content_key
    )                                                            AS row_is_elected_canonical,

    COALESCE(eps.id, epm.id)                                     AS eps_id,
    COALESCE(eps.status, epm.status)                             AS eps_status,
    COALESCE(eps.domain, epm.domain)                             AS eps_domain,
    COALESCE(eps.attached_product_key, epm.attached_product_key) AS eps_attached_product_key,
    COALESCE(eps.updated_at, epm.updated_at)                     AS eps_last_seen_at,
    COALESCE(eps.seed_kind, epm.seed_kind)                       AS eps_seed_kind,

    ms.merchant_id           AS ms_merchant_id,
    ms.platform              AS ms_platform,
    ms.domain                AS ms_domain,
    ms.status                AS ms_status,
    ms.last_sync             AS ms_last_sync,

    pio.id                   AS override_id,
    pio.action_type          AS override_action_type,
    pio.active               AS override_active

  FROM catalog_products cp
  LEFT JOIN index_pipeline_state ips
    ON ips.content_key = cp.content_key
  LEFT JOIN external_seed_one eps
    -- ADR-009: match the external-seed mirror by source_system, NOT the legacy
    -- merchant_id='external_seed' bucket. External seeds now mirror under
    -- per-brand observed sellers (merch_obs_…); the merchant_id conjunct made
    -- their eps join miss (eps NULL), so _derive_source_lifecycle returned
    -- 'unknown' and a disabled merch_obs_ seed never emitted its inactive block.
    ON cp.source_system = 'external_product_seeds_mirror_v1'
   AND eps.external_product_id = cp.source_product_id
  LEFT JOIN minted_seed_one epm
    -- Path-C minted canonicals: seeds attach by attached_product_key (their
    -- source_product_id is a canonical name slug, not a seed id).
    ON cp.source_system = 'catalog_enrichment_agent_v1'
   AND epm.attached_product_key = cp.product_key
  LEFT JOIN pdp_identity_listing pil
    -- Identity rows for external-seed supply are keyed by the SEED id
    -- (pil.product_id = eps.external_product_id). Mirror rows carry that id
    -- in cp.source_product_id; minted rows must route through their attached
    -- seed (epm) or the pil join silently misses and trust re-derives
    -- IDENTITY_CONFIDENCE_NULL forever (the shadow-forever bug on the Jul-16
    -- minted cohort).
    ON pil.merchant_id = cp.merchant_id
   AND pil.product_id = CASE
         WHEN cp.source_system = 'catalog_enrichment_agent_v1'
           THEN epm.external_product_id
         ELSE cp.source_product_id
       END
  LEFT JOIN merchant_store_one ms
    ON ms.merchant_id = cp.merchant_id AND ms.platform = cp.platform
  LEFT JOIN identity_override_one pio
    ON pio.source_listing_ref = pil.source_listing_ref AND pio.active = TRUE
"""


_SELECT_BY_PRODUCT_KEY_SQL = (
    _PRODUCT_JOIN_CTES
    + _PRODUCT_JOIN_SELECT
    + "  WHERE cp.product_key = :product_key\n"
)


_SELECT_BY_PRODUCT_KEYS_SQL = (
    _PRODUCT_JOIN_CTES
    + _PRODUCT_JOIN_SELECT
    + """  WHERE cp.product_key = ANY(:product_keys)
  ORDER BY cp.product_key
  LIMIT :limit
"""
)


# Both sides through `sql_bare_domain` (lowercase + `www.` strip + empty-as-NULL),
# the SAME helper the anti-join and the Python matcher use. A bare `lower()` on
# each side made this lookup disagree with them — see #1639: a `www.mintree.us`
# quarantine, which `create_quarantine` accepts verbatim, was honoured by the
# anti-join and missed here, so the rows were excluded from serving while
# `serving_decision` was never recomputed.
#
# `empty-as-NULL` matters as much as the strip: without it a blank `match_value`
# would select every product whose domain sources are all NULL, and this query
# feeds a trust WRITE.
_SELECT_BY_QUARANTINE_DOMAIN_SQL = (
    _PRODUCT_JOIN_CTES
    + _PRODUCT_JOIN_SELECT
    + f"""  WHERE {sql_bare_domain("coalesce(nullif(cp.source_domain, ''), nullif(eps.domain, ''), nullif(epm.domain, ''), nullif(ms.domain, ''), '')")}
        = {sql_bare_domain(":match_value")}
  ORDER BY cp.product_key
  LIMIT :limit
"""
)


_SELECT_BY_QUARANTINE_MERCHANT_PLATFORM_SQL = (
    _PRODUCT_JOIN_CTES
    + _PRODUCT_JOIN_SELECT
    + """  WHERE cp.merchant_id || ':' || cp.platform = :match_value
  ORDER BY cp.product_key
  LIMIT :limit
"""
)


_SELECT_BY_QUARANTINE_SOURCE_SYSTEM_REF_SQL = (
    _PRODUCT_JOIN_CTES
    + _PRODUCT_JOIN_SELECT
    + """  WHERE cp.source_system || ':' || cp.source_ref = :match_value
  ORDER BY cp.product_key
  LIMIT :limit
"""
)


_UPSERT_SQL = """
  INSERT INTO catalog_row_trust (
    subject_type, subject_key,
    product_key, source_listing_ref, content_key, source_id, source_domain,
    source_lifecycle_state, source_last_checked_at,
    identity_status, identity_confidence,
    matched_product_key, matched_content_key, matched_sellable_item_group_id,
    freshness_state, last_verified_at, verification_source,
    serving_decision, serving_reason_codes,
    manual_override_id, policy_version, updated_at
  ) VALUES (
    :subject_type, :subject_key,
    :product_key, :source_listing_ref, :content_key, :source_id, :source_domain,
    :source_lifecycle_state, :source_last_checked_at,
    :identity_status, :identity_confidence,
    :matched_product_key, :matched_content_key, :matched_sellable_item_group_id,
    :freshness_state, :last_verified_at, :verification_source,
    :serving_decision, :serving_reason_codes,
    :manual_override_id, :policy_version, now()
  )
  ON CONFLICT (subject_type, subject_key) DO UPDATE SET
    product_key = EXCLUDED.product_key,
    source_listing_ref = EXCLUDED.source_listing_ref,
    content_key = EXCLUDED.content_key,
    source_id = EXCLUDED.source_id,
    source_domain = EXCLUDED.source_domain,
    source_lifecycle_state = EXCLUDED.source_lifecycle_state,
    source_last_checked_at = EXCLUDED.source_last_checked_at,
    identity_status = EXCLUDED.identity_status,
    identity_confidence = EXCLUDED.identity_confidence,
    matched_product_key = EXCLUDED.matched_product_key,
    matched_content_key = EXCLUDED.matched_content_key,
    matched_sellable_item_group_id = EXCLUDED.matched_sellable_item_group_id,
    freshness_state = EXCLUDED.freshness_state,
    last_verified_at = EXCLUDED.last_verified_at,
    verification_source = EXCLUDED.verification_source,
    serving_decision = EXCLUDED.serving_decision,
    serving_reason_codes = EXCLUDED.serving_reason_codes,
    manual_override_id = EXCLUDED.manual_override_id,
    policy_version = EXCLUDED.policy_version,
    updated_at = now()
  WHERE catalog_row_trust.policy_version <> EXCLUDED.policy_version
     OR catalog_row_trust.serving_decision <> EXCLUDED.serving_decision
     OR catalog_row_trust.serving_reason_codes <> EXCLUDED.serving_reason_codes
     OR catalog_row_trust.source_lifecycle_state <> EXCLUDED.source_lifecycle_state
     OR catalog_row_trust.identity_status <> EXCLUDED.identity_status
"""


_QUARANTINE_SELECT_SQL = """
  SELECT quarantine_id, match_type, match_value, state, expires_at
  FROM catalog_source_quarantine
  WHERE state = 'active'
    AND (expires_at IS NULL OR expires_at > now())
"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def upsert_catalog_row_trust(
    *,
    db: Any,
    product_key: str,
    now: Optional[datetime] = None,
) -> bool:
    """Recompute and UPSERT the trust row for one ``product_key``.

    Returns True iff a row was assembled and written. Exceptions are logged
    and swallowed — producers must never fail because trust upsert failed
    (the defensive cron in phase 2d catches drift).
    """
    if not product_key:
        return False
    try:
        joined = await _fetch_one(db, _SELECT_BY_PRODUCT_KEY_SQL, {"product_key": product_key})
        if joined is None:
            # Row was deleted between the producer's write and this hook —
            # don't write a phantom trust row.
            logger.debug(
                "catalog_row_trust upsert: product_key=%s not found, skipping",
                product_key,
            )
            return False
        quarantines = await _load_active_quarantines(db)
        return await _compute_and_upsert(
            db=db,
            joined=joined,
            quarantines=quarantines,
            now=now,
        )
    except Exception:  # pragma: no cover — log-and-swallow contract
        logger.exception(
            "catalog_row_trust upsert failed for product_key=%s", product_key
        )
        return False


async def upsert_catalog_row_trust_many(
    *,
    db: Any,
    product_keys: Sequence[str],
    now: Optional[datetime] = None,
    limit: int = _QUARANTINE_BULK_MAX,
) -> int:
    """Recompute and UPSERT trust rows for every supplied ``product_key``.

    ``limit`` bounds each fetch BATCH, not total coverage: the keys are
    processed in the caller's order, ``limit`` at a time, until the list is
    exhausted. The caller's order is load-bearing — the backfill cron passes
    keys stalest-first, and a bound that re-sorted or truncated the list
    would starve the tail (rows past the bound never got a trust row, and
    fail-closed readers made them permanently unservable).

    Returns the count of rows the policy emitted (write attempts; idempotent
    UPSERT may not change all of them). Errors are logged per row and per
    batch; a failed batch never aborts the remaining batches.
    """
    keys = [str(k) for k in product_keys if k]
    if not keys:
        return 0
    batch_size = max(1, int(limit))
    quarantines: Optional[list] = None
    wrote = 0
    no_catalog_row = 0
    for start in range(0, len(keys), batch_size):
        chunk = keys[start : start + batch_size]
        try:
            rows = await _fetch_all(
                db,
                _SELECT_BY_PRODUCT_KEYS_SQL,
                {"product_keys": chunk, "limit": len(chunk)},
            )
            if quarantines is None:
                quarantines = await _load_active_quarantines(db)
            no_catalog_row += max(0, len(chunk) - len(rows))
            for row in rows:
                try:
                    if await _compute_and_upsert(
                        db=db, joined=row, quarantines=quarantines, now=now
                    ):
                        wrote += 1
                except Exception:  # pragma: no cover
                    logger.exception(
                        "catalog_row_trust upsert failed for product_key=%s",
                        _row_get(row, "product_key"),
                    )
        except Exception:  # pragma: no cover
            logger.exception(
                "catalog_row_trust bulk upsert: batch %d-%d of %d keys failed",
                start,
                start + len(chunk),
                len(keys),
            )
    if no_catalog_row:
        # Counted per SUCCESSFUL batch only (failed batches are logged
        # above and say nothing about their keys). product_key is the
        # catalog_products PK, so within a fetched batch a shortfall is
        # exactly the keys with no catalog row anymore (deleted
        # mid-flight). Expected in small numbers; surfaced so a coverage
        # gap is never silent.
        logger.info(
            "catalog_row_trust bulk upsert: %d of %d keys had no catalog row",
            no_catalog_row,
            len(keys),
        )
    return wrote


async def upsert_catalog_row_trust_for_quarantine_match(
    *,
    db: Any,
    match_type: str,
    match_value: str,
    now: Optional[datetime] = None,
    limit: int = _QUARANTINE_BULK_MAX,
) -> int:
    """Recompute trust rows for every catalog product matching a quarantine
    predicate. Use when a quarantine is created or revoked.

    Each predicate has the same matching SQL shape as
    ``services.source_quarantine.quarantine_matches_source``.
    """
    if not match_value:
        return 0
    try:
        if match_type == "domain":
            sql = _SELECT_BY_QUARANTINE_DOMAIN_SQL
        elif match_type == "merchant_platform":
            sql = _SELECT_BY_QUARANTINE_MERCHANT_PLATFORM_SQL
        elif match_type == "source_system_ref":
            sql = _SELECT_BY_QUARANTINE_SOURCE_SYSTEM_REF_SQL
        else:
            logger.warning(
                "catalog_row_trust quarantine refresh: unknown match_type=%s",
                match_type,
            )
            return 0

        rows = await _fetch_all(
            db,
            sql,
            {"match_value": match_value, "limit": int(limit)},
        )
        quarantines = await _load_active_quarantines(db)
        wrote = 0
        for row in rows:
            try:
                if await _compute_and_upsert(
                    db=db, joined=row, quarantines=quarantines, now=now
                ):
                    wrote += 1
            except Exception:  # pragma: no cover
                logger.exception(
                    "catalog_row_trust upsert failed (quarantine refresh) "
                    "for product_key=%s",
                    _row_get(row, "product_key"),
                )
        if len(rows) >= limit:
            logger.warning(
                "catalog_row_trust quarantine refresh hit row cap "
                "(match_type=%s match_value=%s limit=%d) — some rows skipped, "
                "defensive cron will reconcile",
                match_type,
                match_value,
                limit,
            )
        return wrote
    except Exception:  # pragma: no cover
        logger.exception(
            "catalog_row_trust quarantine refresh failed (match_type=%s match_value=%s)",
            match_type,
            match_value,
        )
        return 0


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _compute_and_upsert(
    *,
    db: Any,
    joined: Mapping[str, Any],
    quarantines: list[dict[str, Any]],
    now: Optional[datetime],
) -> bool:
    inputs = _joined_row_to_inputs(joined, quarantines, now)
    if not inputs.get("subject_key"):
        return False
    trust = derive_trust(inputs)
    await _execute(db, _UPSERT_SQL, _trust_to_params(trust))
    return True


def _joined_row_to_inputs(
    row: Mapping[str, Any],
    active_quarantines: list[dict[str, Any]],
    now: Optional[datetime],
) -> dict[str, Any]:
    """Reshape the JOIN row into the ``derive_trust`` input shape.

    Mirrors ``rowToPolicyInputs`` in ``scripts/backfill-catalog-row-trust.cjs``.
    """
    now_value = now if isinstance(now, datetime) else datetime.now(timezone.utc)

    product_key = _row_get(row, "product_key")
    has_identity = _row_get(row, "identity_status") is not None
    has_ips = _row_get(row, "serving_eligible") is not None
    has_seed = _row_get(row, "eps_id") is not None
    has_store = _row_get(row, "ms_merchant_id") is not None
    has_override = _row_get(row, "override_id") is not None

    return {
        "subject_type": "product",
        "subject_key": product_key,
        # c1.v0.5. The lane test is pure row data; only the seed EXISTS needs
        # SQL (pdp_seed_route_ok above). Tri-state: a row assembled without
        # that column (hand-built test inputs, older callers) yields None and
        # leaves the decision exactly as c1.v0.4.
        "pdp_route_resolvable": (
            None
            if _row_get(row, "pdp_seed_route_ok") is None
            else pdp_route_resolvable(
                merchant_id=_row_get(row, "merchant_id"),
                platform=_row_get(row, "platform"),
                source_system=_row_get(row, "source_system"),
                source_product_id=_row_get(row, "source_product_id"),
                seed_route_ok=bool(_row_get(row, "pdp_seed_route_ok")),
            )
        ),
        # Tri-state, same contract as pdp_route_resolvable above: True = this
        # product_key has its own unsuppressed priced offer, False = it does
        # not, None = the caller did not compute it and the gate stays silent.
        # The JOIN always computes it (EXISTS is never NULL), so None here means
        # a hand-built test input or an older caller.
        "row_has_priced_offer": (
            None
            if _row_get(row, "row_has_priced_offer") is None
            else bool(_row_get(row, "row_has_priced_offer"))
        ),
        # Tri-state, same contract again. True = this row IS its content_key's
        # elected canonical, False = a sibling holds the canonical URL, None =
        # no election exists for this content_key and the gate stays silent.
        # Unlike the two above, the JOIN legitimately yields NULL here — the
        # election table does not cover every content_key — so None is a normal
        # production value, not just a hand-built-test artifact.
        "row_is_elected_canonical": (
            None
            if _row_get(row, "row_is_elected_canonical") is None
            else bool(_row_get(row, "row_is_elected_canonical"))
        ),
        "product": {
            "product_key": product_key,
            "content_key": _row_get(row, "content_key"),
            "merchant_id": _row_get(row, "merchant_id"),
            "platform": _row_get(row, "platform"),
            "source_system": _row_get(row, "source_system"),
            "source_ref": _row_get(row, "source_ref"),
            "source_product_id": _row_get(row, "source_product_id"),
            "source_domain": _row_get(row, "source_domain"),
            "sync_status": _row_get(row, "sync_status"),
            "suppression_reason": _row_get(row, "suppression_reason"),
            "last_seen_in_sync_at": _row_get(row, "last_seen_in_sync_at"),
            # seed_kind='cross' (retailer-sourced observed seller) must NOT get the
            # observed-seller public-passthrough exemption; 'self'/NULL keeps it.
            "seed_kind": _row_get(row, "eps_seed_kind"),
        },
        "identity": (
            {
                "source_listing_ref": _row_get(row, "pil_source_listing_ref"),
                "identity_status": _row_get(row, "identity_status"),
                "identity_confidence": _row_get(row, "identity_confidence"),
                "live_read_enabled": _row_get(row, "live_read_enabled"),
                "review_required": _row_get(row, "review_required"),
                "sellable_item_group_id": _row_get(row, "sellable_item_group_id"),
                "product_line_id": _row_get(row, "product_line_id"),
                "review_family_id": _row_get(row, "review_family_id"),
            }
            if has_identity
            else None
        ),
        "ips": (
            {
                "serving_eligible": _row_get(row, "serving_eligible"),
                "pipeline_stage": _row_get(row, "pipeline_stage"),
                "blocker_code": _row_get(row, "blocker_code"),
                "content_quality_score": _row_get(row, "content_quality_score"),
                "quality_scored_at": _row_get(row, "quality_scored_at"),
                "last_extracted_at": _row_get(row, "last_extracted_at"),
            }
            if has_ips
            else None
        ),
        "external_seed": (
            {
                "id": _row_get(row, "eps_id"),
                "status": _row_get(row, "eps_status"),
                "domain": _row_get(row, "eps_domain"),
                "attached_product_key": _row_get(row, "eps_attached_product_key"),
                "last_seen_at": _row_get(row, "eps_last_seen_at"),
            }
            if has_seed
            else None
        ),
        "merchant_store": (
            {
                "merchant_id": _row_get(row, "ms_merchant_id"),
                "platform": _row_get(row, "ms_platform"),
                "domain": _row_get(row, "ms_domain"),
                "status": _row_get(row, "ms_status"),
                "last_sync": _row_get(row, "ms_last_sync"),
            }
            if has_store
            else None
        ),
        "override": (
            {
                "id": _row_get(row, "override_id"),
                "action_type": _row_get(row, "override_action_type"),
                "active": _row_get(row, "override_active"),
            }
            if has_override
            else None
        ),
        "active_quarantines": active_quarantines,
        "now": now_value,
    }


def _trust_to_params(trust: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "subject_type": trust["subject_type"],
        "subject_key": trust["subject_key"],
        "product_key": trust["product_key"],
        "source_listing_ref": trust["source_listing_ref"],
        "content_key": trust["content_key"],
        "source_id": trust["source_id"],
        "source_domain": trust["source_domain"],
        "source_lifecycle_state": trust["source_lifecycle_state"],
        "source_last_checked_at": trust["source_last_checked_at"],
        "identity_status": trust["identity_status"],
        "identity_confidence": trust["identity_confidence"],
        "matched_product_key": trust["matched_product_key"],
        "matched_content_key": trust["matched_content_key"],
        "matched_sellable_item_group_id": trust["matched_sellable_item_group_id"],
        "freshness_state": trust["freshness_state"],
        "last_verified_at": trust["last_verified_at"],
        "verification_source": trust["verification_source"],
        "serving_decision": trust["serving_decision"],
        "serving_reason_codes": list(trust["serving_reason_codes"]),
        "manual_override_id": trust["manual_override_id"],
        "policy_version": trust["policy_version"],
    }


async def _load_active_quarantines(db: Any) -> list[dict[str, Any]]:
    rows = await _fetch_all(db, _QUARANTINE_SELECT_SQL, {})
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "quarantine_id": _row_get(r, "quarantine_id"),
                "match_type": _row_get(r, "match_type"),
                "match_value": _row_get(r, "match_value"),
                "state": _row_get(r, "state"),
                "expires_at": _row_get(r, "expires_at"),
            }
        )
    return out


def _row_get(row: Any, key: str) -> Any:
    if row is None:
        return None
    if isinstance(row, Mapping):
        return row.get(key)
    return getattr(row, key, None)


async def _fetch_one(db: Any, sql: str, values: Mapping[str, Any]) -> Any:
    return await db.fetch_one(sql, dict(values))


async def _fetch_all(db: Any, sql: str, values: Mapping[str, Any]) -> Iterable[Any]:
    rows = await db.fetch_all(sql, dict(values))
    return rows or []


async def _execute(db: Any, sql: str, values: Mapping[str, Any]) -> Any:
    return await db.execute(sql, dict(values))


__all__ = [
    "upsert_catalog_row_trust",
    "upsert_catalog_row_trust_many",
    "upsert_catalog_row_trust_for_quarantine_match",
]
