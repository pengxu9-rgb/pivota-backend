"""Pin the stored strings and live flag names of the reviewed retailer cohorts as LITERALS.

Why literals, and why real names are allowed here: these values are written into, or matched
against, production rows (offer proof key, ingest batch labels, skip reasons, review policy
versions, seed-data proposer and source, evidence references), name Cloud Run jobs, or are
environment flags that are set in production today. Since the cohorts moved into
config/reviewed_retailer_cohorts.json and config/external_seed_review_listings.json, every other
test reads them from that same config, so a config edit would pass CI while orphaning stored rows
or silently turning a live cohort off. This file is the one deliberate exception to the
no-real-names rule for tests: the expected values are the stored values themselves. Change a value
here only together with a migration of the rows or jobs that carry it.
"""

import importlib

from services import exact_retailer_cohorts as cohorts
from services import external_seed_review_pilot as pilot
from services import reviewed_variant_expansion as expansion


def test_live_serving_flag_names_are_unchanged():
    # Both are set on the production web service; a rename would switch the cohort off.
    assert cohorts.PILOT_FLAG == cohorts.RETAILER_COHORT_A.flag == "SURRATT_EXACT_RETAILER_SHADES_ENABLED"
    assert cohorts.COHORT_B_FLAG == cohorts.RETAILER_COHORT_B.flag == "MEITU_US_KIOKII_EXACT_ENABLED"
    assert cohorts.COHORT_B_EXPANSION_FLAG == "KIOKII_US_RETAILER_EXPANSION_ENABLED"


def test_stored_proof_key_labels_and_skip_reasons_are_unchanged():
    assert cohorts.PROOF_KEY == "exact_retailer_shade_v1"
    assert cohorts.COHORT_A_CONFIG["review_label"] == "surratt_exact_retailer_review_v1"
    assert cohorts.COHORT_B_CONFIG["review_label"] == "meitu_us_kiokii_retailer_review_v1"
    assert cohorts.COHORT_A_CONFIG["ingest_skip_reason"] == "reviewed_surratt_retailer_scope"
    assert cohorts.COHORT_B_CONFIG["ingest_skip_reason"] == "reviewed_kiokii_retailer_scope"


def test_refresh_job_settings_are_unchanged():
    a, b = cohorts.COHORT_A_CONFIG["refresh"], cohorts.COHORT_B_CONFIG["refresh"]
    assert (a["module"], a["scope"], a["job_prefix"], a["write_flag"], a["release_commit_env"]) == (
        "scripts.ops.surratt_retailer_offers", "surratt_us_retailer_referrals", "meitu-surratt-retailer-",
        "SURRATT_RETAILER_WRITE_ENABLED", "SURRATT_RELEASE_COMMIT",
    )
    assert (b["module"], b["scope"], b["job_prefix"], b["write_flag"], b["release_commit_env"]) == (
        "scripts.ops.kiokii_us_retailer_offers", "kiokii_us_exact_referrals", "meitu-kiokii-us-retailer-",
        "KIOKII_US_RETAILER_WRITE_ENABLED", "KIOKII_RELEASE_COMMIT",
    )
    job = cohorts.COHORT_B_CONFIG["expansion_job"]
    assert (job["module"], job["scope"], job["enable_flag"], job["job_prefixes"]) == (
        "scripts.ops.amuse_us_six_expansion", "amuse_us_six_expansion", "MEITU_US_KIOKII_SIX_EXPANSION_ENABLED",
        {"staging": "meitu-stage-kiokii-us-six-", "production": "meitu-prod-kiokii-us-six-"},
    )
    # The entrypoints the production jobs run must still import from those module paths.
    for module in (a["module"], b["module"], job["module"]):
        importlib.import_module(module)


def test_review_policy_versions_and_seed_ids_are_unchanged():
    assert pilot.POLICY_VERSION == "olive-young-us-brand-direct-v4"
    assert (pilot.SEED_A_ID, pilot.SEED_B_ID, pilot.SEED_C_ID) == (
        "eps_9f3368aa75d349e09914fbcb", "eps_31f74c1ec20a4d5db0fa977f", "eps_6aa6d3d1e93a424b8c64d6c5",
    )
    assert {seed: listing.policy_version for seed, listing in pilot.LISTINGS.items()} == {
        "eps_31f74c1ec20a4d5db0fa977f": "olive-young-us-brand-direct-v4",
        "eps_6aa6d3d1e93a424b8c64d6c5": "olive-young-us-brand-direct-v4",
        "eps_9f3368aa75d349e09914fbcb": "surratt-us-brand-direct-v1",
    }
    assert pilot.SCOPED_LISTING.policy_version == "surratt-automatique-us-brand-direct-v1"
    assert pilot.SCOPED_EXTERNAL_PRODUCT_ID == "ext_5cb339225f03d6bbbf8d25dc"
    assert pilot.SCOPED_LISTING_PIN_ENV == "EXTERNAL_SEED_MACHINE_REVIEW_AUTOMATIQUE_SEED_ID"


def test_variant_expansion_proposer_source_and_evidence_prefix_are_unchanged():
    assert expansion.POLICY_VERSION == "amuse-us-variant-expansion-v1"
    assert expansion.PROPOSER == "machine_review:amuse_us_variant_expansion"
    assert expansion.PROPOSAL_SOURCE == "meitu_us_official_variant_review"
    assert expansion.RETAILER_EVIDENCE_PREFIX == "kiokii_us"
    assert expansion.ENABLE_FLAG == "MEITU_US_KIOKII_SIX_EXPANSION_ENABLED"
    assert expansion.EXTERNAL_ID == "ext_51577f22556c1f3aeb091391"


def test_cohort_c_stored_strings_and_env_names_are_unchanged():
    # Neutral literals carried by cohort C rows and runtime env; the cohort's data stays private.
    assert cohorts.COHORT_C_REVIEW_LABEL == "reviewed_retailer_c_referrals_v1"
    assert cohorts.COHORT_C_SKIP_REASON == "reviewed_retailer_c_scope"
    assert cohorts.COHORT_C_MANIFEST == "REVIEWED_RETAILER_MANIFEST_PATH"
    assert cohorts.COHORT_C_MANIFEST_SHA256 == "REVIEWED_RETAILER_MANIFEST_SHA256"
    assert cohorts.COHORT_C_FLAG == "REVIEWED_RETAILER_EXACT_ENABLED"
    assert cohorts.COHORT_C_WRITE_FLAG == "REVIEWED_RETAILER_REFRESH_WRITE_ENABLED"
