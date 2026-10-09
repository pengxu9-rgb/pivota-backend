"""A VERIFIED brand storefront's own seed is 'self', not 'cross' -- and nothing else is.

2026-09-28: all 3,540 catalog_row_trust rows shadowed IDENTITY_LIVE_READ_DISABLED were merch_obs_
sellers with seed_kind='cross'; 1,937 of them were the brand's own storefront written by the
retailer-ingest drain. Path C attaches every seed to a synthetic pk_<hash>, so no tenant anchor ever
owned the domain and derive_seed_seller filed the brand's own store as 'cross' -- which strips the
observed-seller identity-coverage exemption in catalog_trust_policy.

Peng (2026-09-29): a brand-official storefront the drain VERIFIED (Tier A, Tier B, or a human-accepted
brand_official_domain_unproven flag) gets 'self' seeds. source_role=brand_official alone is not proof:
the curated onboard queue defaults a role-less job to brand_official and auto-applies with no domain
check, and is_known_retailer misses resellers (luxiface.com, tripletraders.com, perfumania.com,
shoppalacebeauty.com). Every positive case has a refusing twin.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

import pytest

from services import curated_brand_feed as feed
from services import seller_identity as si
from services.catalog_enrichment_agent import apply as writer
from services.catalog_enrichment_agent.apply import VerifiedBrandStorefront
from services.catalog_enrichment_agent.ingestion import ingest_validated_jsonl
from services.offer_seller_identity import is_known_retailer
from services.retailer_ingest import pipeline
from tests.services.test_partial_apply_skip_reasons import CatalogDB, real_resolver  # noqa: F401 -- fixture
from tests.services.test_retailer_ingest_explicit_market import _crawl_with
from tests.services.test_retailer_ingest_pipeline import env, job  # noqa: F401 -- the drain fixture

_REAL_DERIVE_FOR_PLAN_ROW = writer._derive_seed_seller_for_plan_row
_OBSERVED = "merch_obs_0123456789abcdef"


async def _mint(**_: Any) -> str:
    return _OBSERVED


def _verified(host: str, *brands: str) -> VerifiedBrandStorefront:
    return VerifiedBrandStorefront(host=host, brand_labels=frozenset(writer.brand_official_label(b) for b in brands))


# --- derive_seed_seller -------------------------------------------------------------------------


async def _derive(destination: Optional[str], verified: Optional[str]):
    return await si.derive_seed_seller(
        anchor_merchant_id=None, brand="Tarte", source_system="test",
        destination_domain=destination, verified_storefront_domain=verified,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("destination,verified", [
    ("tartecosmetics.com", "tartecosmetics.com"),
    ("https://www.tartecosmetics.com/products/shape-tape", "tartecosmetics.com"),
    ("us.frankbody.com", "us.frankbody.com"),
])
async def test_a_seed_on_the_verified_storefront_is_self(monkeypatch, destination, verified):
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    assert await _derive(destination, verified) == (_OBSERVED, "self")


@pytest.mark.asyncio
@pytest.mark.parametrize("destination,verified", [
    ("tartecosmetics.com", None),                     # nothing verified: today's CROSS
    ("ulta.com", "tartecosmetics.com"),               # the row's offer on a retailer host
    ("sephora.com", "sephora.com"),                   # a known retailer is never a brand's own store
    ("tartecosmetics.co.uk", "tartecosmetics.com"),   # another registrable domain, even the same brand
])
async def test_a_seed_off_the_verified_storefront_stays_cross(monkeypatch, destination, verified):
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    assert await _derive(destination, verified) == (_OBSERVED, "cross")


@pytest.mark.asyncio
async def test_no_destination_is_still_null_on_a_verified_row(monkeypatch):
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    assert await _derive(None, "tartecosmetics.com") == (None, None)


# --- the proof: what the drain recorded on the run ----------------------------------------------


def _brand_official_job(domain: str, **options: Any) -> Dict[str, Any]:
    return {"id": "rij_x", "domain": domain, "brand": "Tarte",
            "options": {"source_role": "brand_official", **options}}


def _evidence(domain: str, **tiers: Optional[str]) -> Dict[str, Any]:
    return {"brand_official_evidence": {"domain": domain, "market": "US",
                                        "brands": {label: {"tier": tier} for label, tier in tiers.items()}}}


@pytest.mark.parametrize("tier", ["A", "B"])
def test_a_tier_a_or_b_brand_is_proven(tier):
    assert pipeline.verified_brand_storefront(
        _brand_official_job("www.Tartecosmetics.com"), _evidence("www.Tartecosmetics.com", tarte=tier),
    ) == VerifiedBrandStorefront(host="tartecosmetics.com", brand_labels=frozenset({"tarte"}))


def test_an_unproven_brand_is_proven_only_by_a_human_accepting_its_flag():
    checks = _evidence("tartecosmetics.com", tarte=None)
    assert pipeline.verified_brand_storefront(_brand_official_job("tartecosmetics.com"), checks) is None
    accepted = _brand_official_job(
        "tartecosmetics.com", accepted_flags=["brand_official_domain_unproven:tartecosmetics.com:tarte"])
    assert pipeline.verified_brand_storefront(accepted, checks) == _verified("tartecosmetics.com", "Tarte")
    # Accepting ANOTHER brand's flag, or the same brand on another domain, proves nothing here.
    other = _brand_official_job("tartecosmetics.com", accepted_flags=[
        "brand_official_domain_unproven:tartecosmetics.com:stila",
        "brand_official_domain_unproven:tarte.co.uk:tarte"])
    assert pipeline.verified_brand_storefront(other, checks) is None


def test_only_the_proven_brands_of_a_mixed_cohort_are_verified():
    proof = pipeline.verified_brand_storefront(
        _brand_official_job("misshaus.com"), _evidence("misshaus.com", missha="A", apieu=None))
    assert proof == _verified("misshaus.com", "Missha")


@pytest.mark.parametrize("job_,checks", [
    # a role-less / retailer job is never a brand's storefront, whatever its evidence says
    ({"domain": "tartecosmetics.com", "options": {}}, _evidence("tartecosmetics.com", tarte="A")),
    ({"domain": "tartecosmetics.com", "options": {"source_role": "retailer"}},
     _evidence("tartecosmetics.com", tarte="A")),
    # a known retailer refused before any tier
    # (with a brand admitted, so the known_retailer marker alone is what refuses it)
    (_brand_official_job("ulta.com"), {"brand_official_evidence": {
        "domain": "ulta.com", "known_retailer": True, "brands": {"tarte": {"tier": "A"}}}}),
    # evidence recorded for a different domain
    (_brand_official_job("tartecosmetics.com"), _evidence("tarte.co.uk", tarte="A")),
    # no review ran (e.g. a run that never reached the check)
    (_brand_official_job("tartecosmetics.com"), {}),
])
def test_nothing_else_is_proof(job_, checks):
    assert pipeline.verified_brand_storefront(job_, checks) is None


# --- the drain hands its proof to the apply (real run_stage) ------------------------------------


def _spy_apply(monkeypatch) -> List[Any]:
    from services.catalog_enrichment_agent import apply as apply_mod
    seen: List[Any] = []
    fake = apply_mod.apply_ingest_plan  # the env fixture's fake

    async def spy(plan, **kw):
        seen.append(kw.get("verified_storefront"))
        return await fake(plan, **kw)
    monkeypatch.setattr(apply_mod, "apply_ingest_plan", spy)
    return seen


async def test_a_tier_a_store_hands_its_proof_to_the_apply(env, monkeypatch):  # noqa: F811
    j = _crawl_with(env, monkeypatch, None, domain="us.frankbody.com", brand="Frank Body")
    j["status"] = "apply_due"
    seen = _spy_apply(monkeypatch)
    await pipeline.run_stage(j, db=env.db)  # the fixture's fake readback then fails the gate; not the subject
    assert seen == [_verified("us.frankbody.com", "Frank Body")]
    run = list(env.ledger.runs.values())[-1]
    assert run["checks"]["verified_storefront"] == {"host": "us.frankbody.com", "brands": ["frank body"]}


async def test_a_human_accepted_store_hands_its_proof_and_an_unaccepted_one_never_applies(env, monkeypatch):  # noqa: F811
    j = _crawl_with(env, monkeypatch, None, domain="tartecosmetics.com", brand="Tarte")
    j["status"] = "apply_due"
    seen = _spy_apply(monkeypatch)
    assert (await pipeline.run_stage(j, db=env.db))["status"] == "held" and seen == []
    j = _crawl_with(env, monkeypatch, None, domain="tartecosmetics.com", brand="Tarte")
    j.update(status="apply_due")
    j["options"]["accepted_flags"] = ["brand_official_domain_unproven:tartecosmetics.com:tarte"]
    await pipeline.run_stage(j, db=env.db)
    assert seen == [_verified("tartecosmetics.com", "Tarte")]


async def test_a_retailer_job_hands_no_proof(env, monkeypatch):  # noqa: F811
    seen = _spy_apply(monkeypatch)
    await pipeline.run_stage(job("apply_due"), db=env.db)
    assert seen == [None]


# --- the real producer: feed record -> ingest plan -> apply -> seed derivation -----------------


def _record(domain: str, *, role: str, brand: str = "Tarte", pid: int = 8101, title: str = "Shape Tape Concealer"):
    return feed.shopify_product_to_record(
        {"id": pid, "vendor": brand, "title": title, "handle": f"p-{pid}",
         "product_type": "Concealer", "body_html": "<p>Ingredients: Water, Glycerin</p>",
         "images": [{"src": f"https://cdn.{domain}/{pid}.jpg"}],
         "variants": [{"id": pid + 1, "price": "32.00", "available": True, "sku": f"S{pid}"}]},
        domain=domain, category_path="beauty", brand_override=brand, currency="USD", source_role=role,
        retailer_name=domain if role == "retailer" else None, emit_native_variants=True)


async def _apply_and_derive(monkeypatch, plan, verified, batch_mode) -> Dict[str, set]:
    """Run the REAL apply and the REAL per-row derivation; return {attached_product_key: {seed_kind}}."""
    derived: Dict[str, set] = {}

    async def recording(seed, **kw):
        result = await _REAL_DERIVE_FOR_PLAN_ROW(seed, **kw)
        derived.setdefault(str(seed.get("attached_product_key")), set()).add(result[1])
        return result
    # real_resolver stubs the seller derivation out; put the real one back, minting only the seller id.
    monkeypatch.setattr(writer, "_derive_seed_seller_for_plan_row", recording)
    monkeypatch.setattr(si, "ensure_observed_seller", _mint)
    assert plan["seeds"], "the producer must plan at least one seed"
    counts = await writer.apply_ingest_plan(plan, batch_label="t", db=CatalogDB(), batch=batch_mode,
                                            verified_storefront=verified)
    assert counts["seeds"] >= 1
    return derived


def _kinds(derived: Dict[str, set]) -> set:
    return set().union(*derived.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_a_verified_brand_storefront_apply_writes_self_seeds(real_resolver, monkeypatch, batch_mode):  # noqa: F811
    plan = ingest_validated_jsonl([_record("tartecosmetics.com", role="brand_official")])
    derived = await _apply_and_derive(monkeypatch, plan, _verified("tartecosmetics.com", "Tarte"), batch_mode)
    assert _kinds(derived) == {"self"}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
@pytest.mark.parametrize("domain,role,verified", [
    # P1: role-only. The curated onboard queue's role-less job is stamped brand_official on a reseller
    # that is NOT on the known-retailer list, and no domain proof ran: 'cross'.
    ("luxiface.com", "brand_official", None),
    ("tartecosmetics.com", "brand_official", None),
    # proof for another host, or for another brand on this host
    ("tartecosmetics.com", "brand_official", _verified("stilacosmetics.com", "Tarte")),
    ("tartecosmetics.com", "brand_official", _verified("tartecosmetics.com", "Stila")),
    # a reseller listing is never a brand storefront row, even on a host carrying proof
    ("shoppalacebeauty.com", "retailer", _verified("shoppalacebeauty.com", "Tarte")),
])
async def test_without_proof_for_this_row_the_apply_writes_cross_seeds(
    real_resolver, monkeypatch, batch_mode, domain, role, verified,  # noqa: F811
):
    plan = ingest_validated_jsonl([_record(domain, role=role)])
    derived = await _apply_and_derive(monkeypatch, plan, verified, batch_mode)
    assert _kinds(derived) == {"cross"}


@pytest.mark.asyncio
@pytest.mark.parametrize("batch_mode", [False, True])
async def test_each_seed_follows_its_own_rows_proof_in_a_mixed_cohort(real_resolver, monkeypatch, batch_mode):  # noqa: F811
    # One store, two brands, only Missha proven (misshaus.com sells A'PIEU too): the A'PIEU row's seeds
    # must not borrow the Missha row's proof.
    missha = _record("misshaus.com", role="brand_official", brand="Missha", pid=9101, title="Time Revolution Essence")
    apieu = _record("misshaus.com", role="brand_official", brand="A'PIEU", pid=9201, title="Madecassoside Cream")
    plan = ingest_validated_jsonl([missha, apieu])
    key_of = {p["brand"]: p["product_key"] for p in plan["pdps"]}
    derived = await _apply_and_derive(monkeypatch, plan, _verified("misshaus.com", "Missha"), batch_mode)
    assert derived[key_of["Missha"]] == {"self"}
    assert derived[key_of["A'PIEU"]] == {"cross"}


def test_the_reseller_hosts_used_here_are_not_on_the_known_retailer_list():
    # Otherwise derive_seed_seller's retailer backstop, not the proof gate, would be what keeps them 'cross'.
    assert not is_known_retailer("luxiface.com") and not is_known_retailer("shoppalacebeauty.com")


def test_verified_storefronts_by_key_maps_only_proven_brand_rows_on_the_verified_host():
    official = ingest_validated_jsonl([_record("tartecosmetics.com", role="brand_official")])["pdps"][0]
    retailer = ingest_validated_jsonl([_record("tartecosmetics.com", role="retailer")])["pdps"][0]
    proof = _verified("tartecosmetics.com", "Tarte")
    assert writer._verified_storefronts_by_key([official, retailer], proof) == {
        official["product_key"]: "tartecosmetics.com"}
    assert writer._verified_storefronts_by_key([official], None) == {}
    # The proven brand on ANOTHER host: this row is not that storefront's (derive_seed_seller's own domain
    # match is a second, independent layer; this one must hold by itself).
    assert writer._verified_storefronts_by_key([official], _verified("stilacosmetics.com", "Tarte")) == {}
