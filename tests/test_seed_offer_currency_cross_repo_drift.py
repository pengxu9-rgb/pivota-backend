"""CROSS-REPO DRIFT PIN (PR controller review of #2657).

The backend and the PIVOTA-Agent gateway each spell the seed-projected offer guard (Peng 2026-10-19:
an offer copied off a seed is a price only in its seed's own currency) in their own language, and
neither CI can read the other repo. So both pin the SAME literal:

  backend: services/seed_offer_currency.py  SEED_PROJECTED_OFFER_SOURCE_SYSTEMS,
           seed_projected_offer_in_seed_currency_sql (seed_currency_sql inside it)
  gateway: src/services/seedSearchOfferScope.js  SEED_PROJECTED_OFFER_SOURCE_SYSTEMS,
           seedProjectedOfferInSeedCurrencySql (seedCurrencySql inside it)
  gateway twin of this test: tests/seed_projected_offer_cross_repo_drift.test.js

A change to either side fails here or there until BOTH literals are updated together, so a PDP or
a purchase (backend) and a search card (gateway) cannot disagree on whether a row is a price.
Whitespace is normalised (the two sides format the statement differently); nothing else is.
"""

import re

from services.seed_offer_currency import (
    SEED_PROJECTED_OFFER_SOURCE_SYSTEMS,
    seed_projected_offer_in_seed_currency_sql,
)

EXPECTED_SOURCE_SYSTEMS = (
    "external_product_seeds_mirror_v1",
    "variant_identity_backfill_v1",
    "variant_offer_projection_v1",
)

# The same segments, in the same order, as the gateway test's EXPECTED_PREDICATE.
EXPECTED_PREDICATE = (
    "(coalesce(o.source_system, '') NOT IN ('external_product_seeds_mirror_v1',"
    " 'variant_identity_backfill_v1', 'variant_offer_projection_v1') OR (SELECT bool_and((CASE WHEN"
    " nullif(upper(trim(eps_own.price_currency)), '') IS NULL THEN"
    " nullif(upper(trim(eps_own.seed_data#>>'{snapshot,price_currency}')), '') ELSE (CASE"
    " coalesce(nullif(upper(trim(eps_own.seed_data#>>'{snapshot,price_currency}')), ''), '') WHEN"
    " nullif(upper(trim(eps_own.price_currency)), '') THEN"
    " nullif(upper(trim(eps_own.price_currency)), '') WHEN '' THEN (CASE WHEN eps_own.seed_data IS"
    " NULL OR (jsonb_typeof(eps_own.seed_data) IN ('object', 'null') AND"
    " (eps_own.seed_data->'snapshot' IS NULL OR jsonb_typeof(eps_own.seed_data->'snapshot') IN"
    " ('object', 'null'))) THEN nullif(upper(trim(eps_own.price_currency)), '') END) END) END) IS NOT"
    " DISTINCT FROM upper(trim(coalesce(o.currency, '')))) FROM external_product_seeds eps_own WHERE"
    " eps_own.attached_product_key = o.product_key AND lower(trim(coalesce(eps_own.status, ''))) ="
    " 'active') IS TRUE)"
)


def _normalise(sql: str) -> str:
    sql = re.sub(r"\s+", " ", sql)
    sql = re.sub(r"\(\s+", "(", sql)
    return re.sub(r"\s+\)", ")", sql).strip()


def test_the_projected_source_systems_are_the_gateways_in_order():
    assert SEED_PROJECTED_OFFER_SOURCE_SYSTEMS == EXPECTED_SOURCE_SYSTEMS


def test_the_predicate_is_the_gateways_statement_whitespace_aside():
    assert _normalise(seed_projected_offer_in_seed_currency_sql("o", "o.product_key")) == EXPECTED_PREDICATE
