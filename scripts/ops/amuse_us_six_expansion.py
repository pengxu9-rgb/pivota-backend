"""Atomically add four reviewed US anchors and their cohort B referral offers.

The job prefixes and the opt-in flag are read from config/reviewed_retailer_cohorts.json
(cohort_b.expansion_job). The database host, database name and runtime service account the job
must find are read from its environment: REVIEWED_JOB_DB_HOST, REVIEWED_JOB_DB_NAME and
REVIEWED_JOB_SERVICE_ACCOUNT. An unset pin holds the run.
"""

import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from db.database import database
from scripts.ops.kiokii_us_retailer_offers import fetch_storefront
from services.reviewed_variant_expansion import ORIGINAL, apply_anchors, prepare_review
from services.catalog_enrichment_agent.reviewed_retailer_apply import apply_reviewed_retailer
from services.exact_retailer_shades import read_offers
from services.outbound_links_service import is_destination_domain_allowed
from services.exact_retailer_cohorts import COHORT_B_CONFIG, RETAILER_COHORT_B


COHORT = RETAILER_COHORT_B
EXPANSION_JOB = COHORT_B_CONFIG["expansion_job"]
PROJECTS = {"staging": "pivota-staging", "production": "pivota-prod"}


def _profile(environment):
    """(project, db host, db name, job prefix, service account); a missing pin is None."""
    if environment not in PROJECTS:
        return (None, None, None, None, None)
    return (PROJECTS[environment], os.getenv("REVIEWED_JOB_DB_HOST") or None,
            os.getenv("REVIEWED_JOB_DB_NAME") or None, EXPANSION_JOB["job_prefixes"][environment],
            os.getenv("REVIEWED_JOB_SERVICE_ACCOUNT") or None)


def metadata(key):
    request = Request("http://metadata.google.internal/computeMetadata/v1/" + key,
                      headers={"Metadata-Flavor": "Google"})
    with urlopen(request, timeout=5) as response:
        if response.headers.get("Metadata-Flavor") != "Google":
            raise ValueError("Cloud Run metadata response lacks identity")
        return response.read(512).decode().strip()


def check_environment(sha):
    environment = os.getenv("PIVOTA_ENV")
    project, host, catalog, prefix, account = _profile(environment)
    dsn = urlsplit(os.environ["DATABASE_URL"])
    job = os.getenv("CLOUD_RUN_JOB", "")
    stamp = Path("/app/.image_commit_sha").read_text().strip()
    if (not re.fullmatch(r"[0-9a-f]{40}", sha) or stamp != sha or not prefix or not job.startswith(prefix)
            or not host or not catalog or not account
            or not os.getenv("CLOUD_RUN_EXECUTION", "").startswith(job + "-")
            or (dsn.hostname, dsn.path) != (host, "/" + catalog)
            or os.getenv("GOOGLE_CLOUD_PROJECT") != project
            or metadata("project/project-id") != project
            or metadata("instance/service-accounts/default/email") != account):
        raise ValueError("expected the pinned cohort B job, backend image, service account, and database")
    if os.getenv(EXPANSION_JOB["enable_flag"]) != "1":
        raise ValueError("seed B six-shade expansion is disabled")
    return environment, host, catalog


async def verify_database(host, catalog):
    if not await database.fetch_val(
        "SELECT host(inet_server_addr())=:host AND current_database()=:db AND NOT pg_is_in_recovery()",
        {"host": host, "db": catalog},
    ):
        raise ValueError("expected the reviewed cohort B database primary")


async def require_domains(listing_url):
    domains = [row["domain"] for row in await database.fetch_all(
        "SELECT domain FROM outbound_link_allowed_domains WHERE market='US' AND status='active'",
    )]
    if any(not is_destination_domain_allowed(destination_url=link, allowed_domains=domains)
           for link in (ORIGINAL.url, listing_url)):
        raise ValueError("the reviewed seed B and cohort B US landing domains must be enabled")


async def verify_retailer_offers(quotes, seller):
    # The reader issues its bounded query in a child task. Use the outer
    # transaction's connection to verify the uncommitted rows.
    connection = database.connection()
    for offer_id, proof in quotes.items():
        actual = {
            (row["merchant_id"], row["offer_id"], row["destination_url"], Decimal(str(row["price_amount"])) * 100)
            for row in await read_offers(connection, proof["canonical_native_variant_id"], timeout=5, cohort=COHORT)
        }
        expected = {(seller, offer_id, proof["retailer_landing_url"], Decimal(proof["price_minor"]))}
        if actual != expected:
            raise ValueError("the exact US retailer reader differs from the reviewed seller, shade or price")
    return sorted(quotes)


async def run(mode, sha):
    environment, host, catalog = check_environment(sha)
    if mode == "plan" and environment != "staging":
        raise ValueError("the rollback simulation is for isolated staging")
    await database.connect()
    try:
        await verify_database(host, catalog)
        record = await fetch_storefront(cohort=COHORT)
        review = await prepare_review(record, review_existing=mode == "plan")
        key, listing = next(iter(COHORT.listings.items()))
        await require_domains(listing.family_url)
        async with database.transaction(force_rollback=mode == "plan"):
            await database.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
            await verify_database(host, catalog)
            await database.execute("SELECT pg_advisory_xact_lock(1160308171, 2)")
            await require_domains(listing.family_url)
            anchor = await apply_anchors(review)
            options = {"cohort": COHORT, "expand_cohort_b": True}
            planned = await apply_reviewed_retailer(database, record, write=False, **options)
            if planned["existing"] != anchor["existing"]:
                raise ValueError("seed B canonical and cohort B offer counts must expand together")
            if (planned["product_key"], planned["seller_ref"], len(planned["offers"])) != (key, listing.merchant_id, 6):
                raise ValueError("exact combined cohort B expansion landed outside its reviewed identity")
            if not planned["replayed"] and (review is None or {
                offer_id: planned["offers"].get(offer_id) for offer_id in review.retailer_quotes
            } != review.retailer_quotes):
                raise ValueError("new cohort B offers differ from the scoped machine review")
            if not planned["replayed"] and await apply_reviewed_retailer(
                database, record, write=True, **options,
            ) != planned:
                raise ValueError("the cohort B write differs from the reviewed plan")
            replay = await apply_reviewed_retailer(database, record, write=True, **options)
            if replay["existing"] != 6 or not replay["replayed"] or planned["offers"] != replay["offers"]:
                raise ValueError("exact combined cohort B expansion is not replay-safe")
            observed_offer_ids = await verify_retailer_offers(planned["offers"], listing.merchant_id)
        return {
            "scope": EXPANSION_JOB["scope"], "schema_version": 1, "market": "US", "mode": mode,
            "environment": environment, "release_sha": sha, "database": catalog,
            "committed": mode == "apply" and not planned["replayed"],
            "same_capture_replayed": True, "canonical": anchor, "listing": planned,
            "offer_readback_ids": observed_offer_ids, "current_review": {
                "evidence_digest": review.evidence_digest,
                "quotes_bound_to_write": not planned["replayed"],
                "models": {stage: {"model": receipt["model_name"], "response_id": receipt["run_id"]}
                           for stage, receipt in review.model_receipts.items() if receipt},
            } if review is not None else None,
        }
    finally:
        await database.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("plan", "apply"))
    parser.add_argument("--expected-image-sha", required=True)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.mode, args.expected_image_sha)), sort_keys=True, default=str), flush=True)
