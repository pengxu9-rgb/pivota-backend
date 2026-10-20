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
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence

from services.catalog_trust_policy import (
    CRAWL_FRESH_DEFAULT_DAYS,
    CRAWL_SOURCE_SYSTEM,
    derive_trust,
    is_crawl_fresh,
    is_crawl_listing,
)
from services.source_quarantine import (
    minted_seed_identity_leg_sql,
    SEED_PICK_ORDER,
    STORE_PICK_ORDER,
    sql_bare_domain,
)
from services.pdp_renderability import (
    pdp_route_resolvable,
    seed_route_resolves_sql,
)
from services.priced_offer_sql import (
    priced_offer_exists_sql,
    priced_offer_row_conjuncts,
)


logger = logging.getLogger(__name__)


# Cap on how many catalog rows we'll bulk-upsert in one quarantine event.
# A run-away predicate (e.g. a wildcard match) should not stall ingest.
_QUARANTINE_BULK_MAX = 5_000


# ---------------------------------------------------------------------------
# SQL — kept literal & in sync with scripts/backfill-catalog-row-trust.cjs.
# Any change here must land in the Node backfill in the same PR set or the
# steady-state and backfill outputs diverge.
# ---------------------------------------------------------------------------


# The identity leg tested against the merchant of the catalog row the seed is
# attached to (`cpx`, joined in the CTE below). Bound here because Python 3.11
# f-string replacement fields cannot contain the same quote character as the
# enclosing string.
_MINTED_SEED_IDENTITY_LEG = minted_seed_identity_leg_sql("cpx.merchant_id")


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
    -- The merchant to test the identity leg against is the one on the catalog
    -- row this seed is attached to; `cp` is not in scope inside a CTE, so join
    -- it. product_key is catalog_products' PRIMARY KEY, so this is 1:1 and
    -- cannot fan out. It REPLACES a `LEFT JOIN pdp_identity_listing spl` that
    -- was dead weight and a fan-out risk: the identity leg declares its own
    -- `spl` inside EXISTS, which shadowed that alias, so the join fed nothing
    -- into the ORDER BY while still multiplying rows before DISTINCT ON.
    -- LEFT, not inner: a seed whose attached_product_key does not resolve keeps
    -- its row and simply loses the identity leg (NULL merchant -> EXISTS false),
    -- which is the correct ranking for a seed that cannot be joined anyway.
    LEFT JOIN catalog_products cpx
      ON cpx.product_key = s.attached_product_key
    WHERE s.attached_product_key IS NOT NULL
    ORDER BY
      s.attached_product_key,
      {_MINTED_SEED_IDENTITY_LEG},
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


# ---------------------------------------------------------------------------
# CRAWL_FRESH input (2026-10-20) — default OFF
# ---------------------------------------------------------------------------
#
# THE SWITCH LIVES HERE, NOT IN THE POLICY. catalog_row_trust is shared state
# written by this service and by the PIVOTA-Agent twin (catalogRowTrustUpserter
# on identity events), so whatever decides a row must be computed identically on
# both sides. The policy stays a pure function of its inputs; this upserter
# decides whether to SUPPLY the `crawl_fresh` input at all.
#
#   * OFF (default): no extra query runs and the input key is never set, so the
#     inputs dict, every decision, every reason code and POLICY_VERSION are
#     byte-identical to main.
#   * ON: drain-lane crawl rows (catalog_trust_policy.is_crawl_listing: retailer
#     listings and brand-official / curated `ext:` canonicals)
#     get `crawl_fresh` = True/False from the row's price-read date below.
#
# 🚨 SET IT ON BOTH SERVICES OR NEITHER, and set the SAME N. With it on one side
# only, this side writes the fresh listings 'public' and the twin re-derives them
# 'shadow' on its next identity event for them: rows FLAP on the live serving
# surface. The rule CATALOG_TRUST_RENDERABLE_GATE documents, unchanged.
#
# Not a separate SELECT column in the product join, deliberately: with the switch
# off the join text stays byte-identical to main (every Postgres gate that pins
# it keeps passing untouched), a database whose catalog_offers lacks
# price_checked_at (migration 246) is never sent a statement naming it, and the
# lookup runs only for the drain-lane rows of a batch, not the whole catalog.
CRAWL_FRESH_ENV = "CATALOG_TRUST_CRAWL_FRESH_ENABLED"
CRAWL_FRESH_DAYS_ENV = "CATALOG_TRUST_CRAWL_FRESH_DAYS"


def crawl_fresh_enabled() -> bool:
    """Default OFF. The same truthy spelling as the policy's other switches."""
    return (os.getenv(CRAWL_FRESH_ENV) or "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def crawl_fresh_days() -> float:
    """N for "crawl-fresh", in days. Unset, unparseable, non-finite or not
    positive -> the default (7). Fractions are allowed (0.5 = 12 hours)."""
    raw = os.getenv(CRAWL_FRESH_DAYS_ENV)
    if raw is None or not raw.strip():
        return float(CRAWL_FRESH_DEFAULT_DAYS)
    try:
        days = float(raw.strip())
    except ValueError:
        days = float("nan")
    if not (days > 0 and days != float("inf")):
        logger.warning(
            "catalog_row_trust: invalid %s=%r; using %d",
            CRAWL_FRESH_DAYS_ENV, raw, CRAWL_FRESH_DEFAULT_DAYS,
        )
        return float(CRAWL_FRESH_DEFAULT_DAYS)
    return days


# An affiliate-feed offer's link is a network tracking URL, not the listing,
# and the drain never read its price from the storefront: never a valid read.
CRAWL_OFFER_NOT_AFFILIATE_SQL = (
    "coalesce(co.offer_payload->>'validated_at', '') NOT LIKE 'affiliate_feed:%'"
)


# ---- rounds 4-5 (2026-10-20): EVERY live offer, fail closed -----------------------
#
# Trust stays per PRODUCT (catalog_row_trust has no market column), and the
# readers serve every live offer of a product to somebody: the buyer-currency
# filter (routes/agent_shop_gateway._offers_in_buyer_currency) keeps offers by
# currency only and, for a caller that declares no market, keeps them all;
# copies the attached-listing lane priced (any source_system) and market
# siblings serve beside the drain's. So the row may go public only when NO
# offer any reader can show carries a price we cannot vouch for. The input is
# the row's price-read date: the OLDEST valid read across EVERY live offer,
# NULL as soon as one of them has none.
#
# THE CLASS -- every LIVE offer on the row (crawl_offer_class_conjuncts):
# unsuppressed and priced (services.priced_offer_sql, the SAME conjuncts every
# price gate uses). Nothing else: ANY source_system, ANY market, ANY currency,
# affiliate offers included, and ANY link. (Round 3 narrowed the class to
# drain-lane, non-affiliate offers in a served market/currency pair, so an
# offer outside it neither dated nor blocked the row -- while a no-market
# caller was still served its price.)
#   * No link test (round 5): the readers do not all test the link. The
#     gateway's offer read keeps only an http(s) destination, but the canonical
#     PDP's own-money read (PIVOTA-Agent canonicalPdpOwnMoney) needs only a
#     public row and serves the price of an offer whose destination is '' or a
#     bare path. So an unservable link never takes an offer out of the class:
#     it must carry a valid read like any other, or the row is not fresh. The
#     link is not part of VALID either -- a drain offer with a bad link whose
#     price was read is still a read.
#
# A VALID READ of one class offer (CRAWL_OFFER_VALID_READ_SQL), ALL of:
#   * written by the drain (source_system catalog_enrichment_agent_v1): no other
#     writer's date is a storefront read this rule vouches for (a variant copy,
#     a market sibling, a mirror row);
#   * in a SERVED market priced in that market's currency
#     (CRAWL_SERVED_MARKET_CURRENCY_PAIRS: config/market_registry.json's served
#     markets x region_pricing's currencies -- the REGISTRY, not the env
#     PIVOTA_SERVING_PRICING_REGIONS the serving index reads; the pair list is
#     committed as config/crawl_fresh_served_markets.json for the twin to pin).
#     An offer in a market we do not serve, or in another currency than its
#     market's, is still shown to a no-market caller, so it is never valid;
#   * not an affiliate-feed offer;
#   * dated by price_checked_at, not after the bound :now (a future stamp is
#     stale -- per offer, so it can never mask a sibling either);
#   * its market was DECLARED by the writer: offer_payload.declared_market equals
#     catalog_offers.market. The column is NOT NULL DEFAULT 'US' (mig 149), so a
#     market-less write lands as 'US' whatever the store's market; only the
#     retailer-ingest drain stamps the key, and only when its job carried an
#     EXPLICIT options.market (retailer_ingest.pipeline.stamp_declared_market;
#     job_market()'s US fallback never stamps). Nothing here ever defaults a
#     market: an undeclared offer is not fresh until a declared crawl re-writes
#     it;
#   * not dated by an employee price edit (offer_payload.price_read_source =
#     'employee_edit', the attached-listing lane's marker). A NULL source -- the
#     drain apply, or any stamp from before the marker -- counts as a crawl.
# So ONE live offer that is not the drain's, not in a served pair, priced in
# the wrong currency, an affiliate offer, undeclared, unread, employee-edited or
# future-dated makes the row NOT fresh (NULL): fail closed.
#
# THE ROW IS CRAWL-FRESH when it has at least one class offer, EVERY class offer
# has a valid read, at least one class offer is not explicitly out of stock, and
# the oldest of those reads is within N days (catalog_trust_policy.
# is_crawl_fresh, judged against the SAME :now the statement was bound with).
#   * cross-market: a fresh SG read never carries a stale US offer;
#   * variants: every live variant offer must be fresh, and `::canonical` too --
#     the refresh dates `::canonical` even when it skipped variants
#     (`variant_not_re_read`), which simply leaves the row stale;
#   * availability NEVER excuses an offer from freshness: an out-of-stock offer
#     is in the class and needs its valid read like any other (the attached lane
#     never updates availability, so its flag can be as old as the drain apply
#     that wrote it). Availability only adds the "at least one in stock" test.
#     That test runs over the whole class, which here is the same as over the
#     VALID offers: a row with any non-valid class offer is NULL already, so
#     whenever the date survives, every class offer is a valid one.
CRAWL_DECLARED_MARKET_KEY = "declared_market"


def _served_market_currency_pairs() -> tuple:
    from services.market_registry import served_markets
    from services.region_pricing import pricing_currency_for_region

    return tuple(sorted((m, pricing_currency_for_region(m)) for m in served_markets()))


#: The served (market, currency) pairs, the twin's pin target
#: (config/crawl_fresh_served_markets.json, scripts/generate_crawl_fresh_served_markets.py).
CRAWL_SERVED_MARKET_CURRENCY_PAIRS = _served_market_currency_pairs()
CRAWL_OFFER_SERVED_MARKET_SQL = (
    "(upper(btrim(coalesce(co.market, ''))), upper(btrim(coalesce(co.currency, ''))))"
    " IN (" + ", ".join(f"('{m}', '{c}')" for m, c in CRAWL_SERVED_MARKET_CURRENCY_PAIRS) + ")"
)


def _unavailable_availabilities() -> list[str]:
    from services.offer_buyability import OFFER_UNAVAILABLE_AVAILABILITIES

    return sorted(OFFER_UNAVAILABLE_AVAILABILITIES)


CRAWL_OFFER_IN_STOCK_SQL = (
    "lower(btrim(coalesce(co.availability, '')))"
    " NOT IN (" + ", ".join(f"'{a}'" for a in _unavailable_availabilities()) + ")"
)
# The key services/external_offer_dual_write writes (PRICE_READ_SOURCE_KEY there;
# pinned equal by test) and the one source that is not a crawl.
CRAWL_PRICE_READ_SOURCE_KEY = "price_read_source"
CRAWL_OFFER_NOT_EMPLOYEE_EDIT_SQL = (
    f"coalesce(co.offer_payload->>'{CRAWL_PRICE_READ_SOURCE_KEY}', '') <> 'employee_edit'"
)
CRAWL_OFFER_DECLARED_MARKET_SQL = (
    f"upper(btrim(coalesce(co.offer_payload->>'{CRAWL_DECLARED_MARKET_KEY}', '')))"
    " = upper(btrim(coalesce(co.market, '')))"
)
CRAWL_OFFER_DRAIN_SOURCE_SQL = f"co.source_system = '{CRAWL_SOURCE_SYSTEM}'"
CRAWL_OFFER_VALID_READ_SQL = (
    f"CASE WHEN {CRAWL_OFFER_DRAIN_SOURCE_SQL}"
    f" AND {CRAWL_OFFER_SERVED_MARKET_SQL}"
    f" AND {CRAWL_OFFER_NOT_AFFILIATE_SQL}"
    " AND co.price_checked_at <= CAST(:now AS timestamptz)"
    f" AND {CRAWL_OFFER_DECLARED_MARKET_SQL}"
    f" AND {CRAWL_OFFER_NOT_EMPLOYEE_EDIT_SQL}"
    " THEN co.price_checked_at END"
)


def crawl_offer_class_conjuncts(product_key_expr: str) -> list[str]:
    """The class for one row: every unsuppressed, priced offer, whatever its link
    (see above). `product_key_expr` is SQL."""
    return [f"co.product_key = {product_key_expr}"] + list(priced_offer_row_conjuncts(alias="co"))


def crawl_row_price_read_lateral_sql(product_key_expr: str, indent: str = "   ") -> str:
    """The row's price-read date as the body of a LATERAL: the oldest valid read
    across every class offer, or NULL. Shared by the input statement and the
    recheck so they cannot disagree. Binds :now."""
    where = f"\n{indent}   AND ".join(crawl_offer_class_conjuncts(product_key_expr))
    return (
        f"{indent}SELECT CASE WHEN count(*) > 0\n"
        f"{indent}             AND bool_and(({CRAWL_OFFER_VALID_READ_SQL}) IS NOT NULL)\n"
        f"{indent}             AND bool_or({CRAWL_OFFER_IN_STOCK_SQL})\n"
        f"{indent}            THEN min({CRAWL_OFFER_VALID_READ_SQL}) END AS row_price_read_at\n"
        f"{indent}  FROM catalog_offers co\n"
        f"{indent} WHERE {where}\n"
    )


# One indexed probe per key (idx_catalog_offers_product_key), as a LATERAL over the
# key list rather than `product_key = ANY(:keys) GROUP BY`: measured on local PG
# (100k products, 300k offers) the ANY form flips to a parallel SEQ SCAN of
# catalog_offers at a backfill batch of 5,000 keys; the lateral stays index probes.
# Rounds 4-5 widened the class (no source_system / market / affiliate / link
# conjunct in the WHERE): the probe is still the product_key equality, and every
# other test filters or aggregates that one key's offers.
#
# WHO DATES THESE OFFERS (every writer of price_checked_at that can reach a
# catalog_enrichment_agent_v1 offer on an `ext:` row):
#   * the drain apply, `apply_ingest_plan(price_read=True)` -- the storefront
#     crawl itself;
#   * the attached-listing lane (services/external_offer_dual_write
#     ATTACHED_LISTING_OFFER_UPDATE_SQL): the external-referral refresh, after
#     it re-read the served page from the origin AND the page named the
#     currency (price_read_source 'refresh'), or an employee price edit on the
#     PATCH route ('employee_edit', not a crawl);
#   * scripts/backfill_offer_price_checked_at (one-off, fill-only): the seed's
#     last_crawled_at, only where the offer's price IS that read's.
# So "fresh" means "the listing page was read recently" (by the drain or by the
# refresh), not "the drain re-crawled it". The same lane also re-prices offers
# other writers own (its offer read has no source_system filter, e.g. variant
# copies): their dates are never valid reads, and such an offer, being live,
# keeps the row NULL.
#
# ONE CLOCK. :now is bound from Python, once per batch, and the age is judged in
# Python against that same value (round 3): SQL now() and the app clock can no
# longer disagree about a row.
CRAWL_ROW_PRICE_READ_SQL = (
    "SELECT k.product_key, r.row_price_read_at\n"
    "  FROM unnest(CAST(:product_keys AS text[])) AS k(product_key)\n"
    " CROSS JOIN LATERAL (\n"
    + crawl_row_price_read_lateral_sql("k.product_key")
    + " ) r\n"
    " WHERE r.row_price_read_at IS NOT NULL\n"
)


# ---- the decay recheck -------------------------------------------------------
#
# A CRAWL_FRESH row stays public only until something re-derives it after its
# read ages past N. The 6h cron's oldest-first pass cannot be that something:
# the UPSERT writes only on a verdict change, so unchanged rows keep old
# updated_at values and stay at the head of the queue while a freshly flipped
# row goes to the back; above CATALOG_TRUST_BACKFILL_LIMIT catalog rows it is
# never reached again. So a dedicated HOURLY job
# (jobs/catalog_row_trust_backfill_cron.run_catalog_row_trust_crawl_fresh_recheck_tick,
# scheduled at :47 by services/audit_scheduler; the 6h tick runs it first too)
# re-derives, bounded and through the GIN index on serving_reason_codes (mig 136):
#   * switch ON: the CRAWL_FRESH rows whose price-read date is missing or older
#     than now - N (they have expired);
#   * switch OFF: EVERY CRAWL_FRESH row, so turning the switch off rolls the
#     rule back on the next run (a no-op read while none exist).
# Both join catalog_products, so a trust row whose product is gone cannot hold a
# slot forever; those orphans are demoted first by a bounded direct trust write
# (_CRAWL_FRESH_ORPHAN_DEMOTE_SQL) to blocked / ROW_TOMBSTONED.
#
# THE REAL BOUND. A row expires at read + N; it is re-derived by the first run
# after that: public at most N + 1h, + the 30 min misfire grace. Only while the
# scheduler is up on the worker AND CATALOG_TRUST_BACKFILL_ENABLED is not off --
# with either down, decay stops and rows stay public until an identity event, a
# drain apply or a re-run touches them. One run re-derives at most
# CATALOG_TRUST_CRAWL_FRESH_RECHECK_LIMIT rows (default 20,000), so the
# flag-off rollback is complete in one run only below that many CRAWL_FRESH rows.
CRAWL_FRESH_RECHECK_LIMIT_ENV = "CATALOG_TRUST_CRAWL_FRESH_RECHECK_LIMIT"
CRAWL_FRESH_RECHECK_DEFAULT_LIMIT = 20_000
CRAWL_FRESH_RECHECK_SQL = (
    "SELECT crt.subject_key\n"
    "  FROM catalog_row_trust crt\n"
    "  JOIN catalog_products cp ON cp.product_key = crt.subject_key\n"
    "  LEFT JOIN LATERAL (\n"
    + crawl_row_price_read_lateral_sql("crt.subject_key")
    + "  ) r ON TRUE\n"
    " WHERE crt.subject_type = 'product'\n"
    "   AND crt.serving_reason_codes @> CAST(ARRAY['CRAWL_FRESH'] AS text[])\n"
    "   AND (r.row_price_read_at IS NULL\n"
    "        OR r.row_price_read_at < CAST(:cutoff AS timestamptz))\n"
    " ORDER BY crt.updated_at ASC NULLS FIRST, crt.subject_key\n"
    " LIMIT :limit\n"
)
# Presence only -- the switched-off rollback must not depend on migration 246.
_CRAWL_FRESH_ROWS_ALL_SQL = (
    "SELECT crt.subject_key\n"
    "  FROM catalog_row_trust crt\n"
    "  JOIN catalog_products cp ON cp.product_key = crt.subject_key\n"
    " WHERE crt.subject_type = 'product'\n"
    "   AND crt.serving_reason_codes @> CAST(ARRAY['CRAWL_FRESH'] AS text[])\n"
    " ORDER BY crt.updated_at ASC NULLS FIRST, crt.subject_key\n"
    " LIMIT :limit\n"
)
# A CRAWL_FRESH trust row whose catalog row is gone: no policy can re-derive it
# (the product join finds nothing), so it is demoted directly. ROW_TOMBSTONED is
# the vocabulary's word for "this catalog row no longer stands"; RETURNING, so
# the count is of rows changed (databases' execute reports none).
#
# The outer WHERE re-states BOTH conditions on the row it updates. Under READ
# COMMITTED a row the subquery picked that a concurrent writer changed is
# re-checked (EvalPlanQual) against the outer quals on its NEW version, so a
# trust row re-derived meanwhile (its catalog row re-created and re-upserted:
# no longer CRAWL_FRESH) is left alone instead of tombstoned. The NOT EXISTS is
# re-stated too; it reads the statement's snapshot, so on its own it cannot see
# a catalog row committed after the statement began -- the reason-code recheck
# is what covers that race once the upserter has re-derived the row.
_CRAWL_FRESH_ORPHAN_DEMOTE_SQL = (
    "UPDATE catalog_row_trust crt\n"
    "   SET serving_decision = 'blocked',\n"
    "       serving_reason_codes = CAST(ARRAY['ROW_TOMBSTONED'] AS text[]),\n"
    "       source_lifecycle_state = 'tombstoned',\n"
    "       updated_at = now()\n"
    " WHERE crt.subject_type = 'product'\n"
    "   AND crt.serving_reason_codes @> CAST(ARRAY['CRAWL_FRESH'] AS text[])\n"
    "   AND NOT EXISTS (SELECT 1 FROM catalog_products cp WHERE cp.product_key = crt.subject_key)\n"
    "   AND crt.subject_key IN (\n"
    "     SELECT o.subject_key FROM catalog_row_trust o\n"
    "      WHERE o.subject_type = 'product'\n"
    "        AND o.serving_reason_codes @> CAST(ARRAY['CRAWL_FRESH'] AS text[])\n"
    "        AND NOT EXISTS (SELECT 1 FROM catalog_products cp WHERE cp.product_key = o.subject_key)\n"
    "      ORDER BY o.subject_key\n"
    "      LIMIT :limit)\n"
    " RETURNING crt.subject_key\n"
)


def crawl_fresh_recheck_limit() -> int:
    raw = (os.getenv(CRAWL_FRESH_RECHECK_LIMIT_ENV) or "").strip()
    try:
        value = int(raw) if raw else CRAWL_FRESH_RECHECK_DEFAULT_LIMIT
    except ValueError:
        return CRAWL_FRESH_RECHECK_DEFAULT_LIMIT
    return value if value > 0 else CRAWL_FRESH_RECHECK_DEFAULT_LIMIT


def _utc(now: Optional[datetime]) -> datetime:
    """`now`, UTC-aware: a naive value is read as UTC; None is the current time."""
    if not isinstance(now, datetime):
        return datetime.now(timezone.utc)
    return now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)


async def recheck_crawl_fresh_rows(
    *,
    db: Any,
    now: Optional[datetime] = None,
    limit: Optional[int] = None,
) -> list[str]:
    """Re-derive the CRAWL_FRESH rows that have aged out (see above), after
    demoting orphans. Returns the keys actually re-derived -- a key whose batch
    failed is NOT returned, so the caller's own pass still covers it. Raises on
    a failed selection read; the cron logs it."""
    now_value = _utc(now)
    bound = int(limit) if limit else crawl_fresh_recheck_limit()
    orphans = await _fetch_all(db, _CRAWL_FRESH_ORPHAN_DEMOTE_SQL, {"limit": bound})
    if orphans:
        logger.warning(
            "catalog_row_trust_crawl_fresh_orphans_demoted count=%d", len(orphans),
        )
    if crawl_fresh_enabled() and await _price_check_column_present(db):
        cutoff = now_value - timedelta(days=crawl_fresh_days())
        rows = await _fetch_all(
            db, CRAWL_FRESH_RECHECK_SQL,
            {"cutoff": cutoff, "now": now_value, "limit": bound},
        )
    else:
        rows = await _fetch_all(db, _CRAWL_FRESH_ROWS_ALL_SQL, {"limit": bound})
    keys = [str(_row_get(r, "subject_key")) for r in rows if _row_get(r, "subject_key")]
    written: list[str] = []
    if keys:
        await upsert_catalog_row_trust_many(db=db, product_keys=keys, now=now, written_keys=written)
    return written


def log_crawl_fresh_config(component: str) -> None:
    """One WARNING line at startup with the effective switch and N, so a flip is
    verifiable from the logs of every runtime (web, worker, the drain job).
    Through the pivota logger: prod's root is at WARNING and drops module INFO."""
    try:
        from utils.logger import logger as operator_logger
    except Exception:  # noqa: BLE001
        operator_logger = logger
    operator_logger.warning(
        "catalog_row_trust_crawl_fresh_config component=%s enabled=%s days=%s recheck_limit=%d "
        "served_market_currency=%s",
        component, crawl_fresh_enabled(), crawl_fresh_days(), crawl_fresh_recheck_limit(),
        ",".join(f"{m}:{c}" for m, c in CRAWL_SERVED_MARKET_CURRENCY_PAIRS),
    )



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
        crawl_fresh = await _crawl_fresh_by_key(db, [joined], now)
        return await _compute_and_upsert(
            db=db,
            joined=joined,
            quarantines=quarantines,
            now=now,
            crawl_fresh=crawl_fresh,
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
    written_keys: Optional[list] = None,
) -> int:
    """Recompute and UPSERT trust rows for every supplied ``product_key``.

    ``written_keys``, when given, collects every key whose row was written, so
    a caller can tell a key that failed (its batch or its row) from one done.

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
            crawl_fresh = await _crawl_fresh_by_key(db, rows, now)
            no_catalog_row += max(0, len(chunk) - len(rows))
            for row in rows:
                try:
                    if await _compute_and_upsert(
                        db=db, joined=row, quarantines=quarantines, now=now,
                        crawl_fresh=crawl_fresh,
                    ):
                        wrote += 1
                        if written_keys is not None:
                            written_keys.append(str(_row_get(row, "product_key")))
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


async def preview_serving_decisions(
    *,
    db: Any,
    product_keys: Sequence[str],
    row_is_elected_canonical: Optional[bool] = None,
    now: Optional[datetime] = None,
) -> dict[str, str]:
    """``serving_decision`` per product_key as an upsert would write it NOW, writing
    nothing. Same join, same input shape, same policy as the upsert.

    ``row_is_elected_canonical`` replaces that one input for every key: "would this
    row be public if it held its content_key's canonical URL?". That is the question
    a caller asks before handing the URL over (scripts/retire_superseded_brand_keys.py),
    and asking the policy rather than reading NON_CANONICAL_DUPLICATE off a stored
    trust row keeps the rule in one place.

    Raises on a failed read, unlike the upserts: a caller deciding on the answer must
    not mistake a failure for "not public" or for "public".
    """
    keys = [str(k) for k in product_keys if k]
    if not keys:
        return {}
    rows = await _fetch_all(
        db, _SELECT_BY_PRODUCT_KEYS_SQL, {"product_keys": keys, "limit": len(keys)}
    )
    quarantines = await _load_active_quarantines(db)
    crawl_fresh = await _crawl_fresh_by_key(db, rows, now)
    out: dict[str, str] = {}
    for row in rows:
        inputs = _joined_row_to_inputs(row, quarantines, now, crawl_fresh=crawl_fresh)
        if not inputs.get("subject_key"):
            continue
        if row_is_elected_canonical is not None:
            inputs["row_is_elected_canonical"] = row_is_elected_canonical
        out[inputs["subject_key"]] = derive_trust(inputs)["serving_decision"]
    return out


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
        crawl_fresh = await _crawl_fresh_by_key(db, rows, now)
        wrote = 0
        for row in rows:
            try:
                if await _compute_and_upsert(
                    db=db, joined=row, quarantines=quarantines, now=now,
                    crawl_fresh=crawl_fresh,
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
    crawl_fresh: Optional[Mapping[str, bool]] = None,
) -> bool:
    inputs = _joined_row_to_inputs(joined, quarantines, now, crawl_fresh=crawl_fresh)
    if not inputs.get("subject_key"):
        return False
    trust = derive_trust(inputs)
    await _execute(db, _UPSERT_SQL, _trust_to_params(trust))
    return True


def _joined_row_to_inputs(
    row: Mapping[str, Any],
    active_quarantines: list[dict[str, Any]],
    now: Optional[datetime],
    *,
    crawl_fresh: Optional[Mapping[str, bool]] = None,
) -> dict[str, Any]:
    """Reshape the JOIN row into the ``derive_trust`` input shape.

    Mirrors ``rowToPolicyInputs`` in ``scripts/backfill-catalog-row-trust.cjs``.

    ``crawl_fresh`` is :func:`_crawl_fresh_by_key`'s answer for the
    batch. None (the switch off, or no price_checked_at column) and a key absent
    from it both leave ``crawl_fresh`` UNSET, so the dict is exactly
    what main built.
    """
    now_value = now if isinstance(now, datetime) else datetime.now(timezone.utc)

    product_key = _row_get(row, "product_key")
    has_identity = _row_get(row, "identity_status") is not None
    has_ips = _row_get(row, "serving_eligible") is not None
    has_seed = _row_get(row, "eps_id") is not None
    has_store = _row_get(row, "ms_merchant_id") is not None
    has_override = _row_get(row, "override_id") is not None

    inputs = {
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
    if crawl_fresh is not None and product_key in crawl_fresh:
        inputs["crawl_fresh"] = bool(crawl_fresh[product_key])
    return inputs


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


async def _crawl_fresh_by_key(
    db: Any,
    rows: Iterable[Any],
    now: Optional[datetime],
) -> Optional[dict[str, bool]]:
    """``{product_key: crawl-fresh?}`` for the drain-lane crawl rows among
    ``rows``, or None when the input must not be supplied at all.

    None (-> the flag-off verdict for every row of the batch) when the switch is
    off, when this database's catalog_offers has no price_checked_at yet (asked
    first, so no statement naming a missing column is ever sent), and when the
    read FAILS: fail closed per batch, log `catalog_row_trust_crawl_fresh_read_failed`,
    and let the caller write the batch anyway (the PIVOTA-Agent twin does the
    same). Both reads run inside a savepoint (`databases`' nested transaction; a
    plain transaction when none is open), so a failed statement never aborts the
    caller's transaction. Rows that are not drain-lane crawl rows never appear in
    the map; a crawl row with no fresh read maps to False (today's gates).

    `now` is bound ONCE for the batch: the statement's :now and the age test use
    the same value. A naive datetime is read as UTC.
    """
    if not crawl_fresh_enabled():
        return None
    keys = sorted(
        {
            str(_row_get(r, "product_key"))
            for r in rows
            if is_crawl_listing(
                {
                    "product_key": _row_get(r, "product_key"),
                    "source_system": _row_get(r, "source_system"),
                }
            )
        }
    )
    if not keys:
        return {}
    now_value = _utc(now)
    try:
        if not await _price_check_column_present(db):
            return None
        fetched = await _in_savepoint(
            db, _fetch_all, CRAWL_ROW_PRICE_READ_SQL, {"product_keys": keys, "now": now_value}
        )
    except Exception:  # noqa: BLE001 -- fail closed per batch, never into the caller
        logger.warning(
            "catalog_row_trust_crawl_fresh_read_failed keys=%d; writing the batch with flag-off verdicts",
            len(keys),
            exc_info=True,
        )
        return None
    max_age_days = crawl_fresh_days()
    reads = {str(_row_get(r, "product_key")): _row_get(r, "row_price_read_at") for r in fetched}
    return {
        pk: is_crawl_fresh(reads.get(pk), now=now_value, max_age_days=max_age_days)
        for pk in keys
    }


async def _in_savepoint(db: Any, fn: Any, *args: Any) -> Any:
    """Run one read inside `db.transaction()` when the handle has one: a SAVEPOINT
    inside an open transaction, its own transaction otherwise. A failure rolls
    back only the savepoint and is re-raised to the caller, who decides."""
    transaction = getattr(db, "transaction", None)
    if transaction is None:
        return await fn(db, *args)
    async with transaction():
        return await fn(db, *args)


async def _price_check_column_present(db: Any) -> bool:
    """Does THIS database's catalog_offers carry price_checked_at (mig 246)?

    Asked of the caller's own connection (the apply's readiness handoff runs in
    its own), with the column probe external_offer_dual_write owns, inside a
    savepoint. Never raises: a failed read (SQLite has no pg_attribute) is
    "absent". Not cached: one catalog lookup per batch, only while the switch is on.
    """
    from services.external_offer_dual_write import PRICE_CHECK_COLUMN_SQL

    try:
        row = await _in_savepoint(db, _fetch_one, PRICE_CHECK_COLUMN_SQL, {})
        return bool(row is not None and _row_get(row, "present"))
    except Exception:  # noqa: BLE001
        return False


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
    "CRAWL_FRESH_RECHECK_SQL",
    "CRAWL_ROW_PRICE_READ_SQL",
    "CRAWL_SERVED_MARKET_CURRENCY_PAIRS",
    "crawl_row_price_read_lateral_sql",
    "log_crawl_fresh_config",
    "recheck_crawl_fresh_rows",
    "CRAWL_FRESH_ENV",
    "CRAWL_FRESH_DAYS_ENV",
    "preview_serving_decisions",
    "crawl_fresh_days",
    "crawl_fresh_enabled",
    "upsert_catalog_row_trust",
    "upsert_catalog_row_trust_many",
    "upsert_catalog_row_trust_for_quarantine_match",
]
