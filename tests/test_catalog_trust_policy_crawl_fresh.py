"""CRAWL_FRESH: a drain-lane storefront crawl row (a retailer listing, or a
brand-official / curated `ext:` canonical) whose listing price was read
recently is exempt from the identity-COVERAGE gates while it stays fresh.

Policy half only (``services.catalog_trust_policy``). The switch and the input's
SQL live in the upserter and are covered by
tests/test_catalog_row_trust_upserter_crawl_fresh.py and
tests/test_crawl_fresh_trust_postgres.py.

The contract pinned here, case by case, so the PIVOTA-Agent twin can mirror it:
  * only an explicit ``crawl_fresh=True`` on a drain-lane crawl row acts; False and absent are today's decision, byte for byte;
  * it removes IDENTITY_CONFIDENCE_NULL, IDENTITY_LIVE_READ_DISABLED and the
    silent identity-'unknown' shadow, and records CRAWL_FRESH only
    where it removed one of them;
  * it never removes review, conflict, non-canonical or any blocked cause, and
    never promotes a row whose PDP is known not to render;
  * first-party and observed-seller ('self') rows are untouched.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from services.catalog_trust_policy import (
    POLICY_VERSION,
    REASON_CODES,
    REASON_CODE_VOCABULARY,
    CRAWL_FRESH_DEFAULT_DAYS,
    CRAWL_LISTING_KEY_PREFIX,
    CRAWL_SOURCE_SYSTEM,
    derive_trust,
    is_crawl_fresh,
    is_crawl_listing,
)

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
RETAILER_PK = "ext:retailer:0123456789abcdef0123456789abcdef"
COVERAGE_CODES = {
    REASON_CODES.IDENTITY_CONFIDENCE_NULL,
    REASON_CODES.IDENTITY_LIVE_READ_DISABLED,
}


def retailer_product(**overrides):
    base = {
        "product_key": RETAILER_PK,
        "content_key": "ck_retailer_1",
        "merchant_id": "merch_obs_0123456789abcdef",
        "platform": "external_seed",
        "source_system": "catalog_enrichment_agent_v1",
        "source_ref": "https://shop.example.com/products/serum",
        "source_product_id": "serum-slug",
        "source_domain": "shop.example.com",
        "sync_status": "live",
        "suppression_reason": None,
        "last_seen_in_sync_at": NOW - timedelta(days=1),
        # The drain's seeds are tagged 'cross': a retailer is not the brand.
        "seed_kind": "cross",
    }
    base.update(overrides)
    return base


def eligible_ips():
    return {
        "serving_eligible": True,
        "pipeline_stage": "serving",
        "blocker_code": None,
        "content_quality_score": 0.8,
        "quality_scored_at": NOW - timedelta(days=1),
        "last_extracted_at": NOW - timedelta(days=1),
    }


def active_seed():
    return {
        "id": 77,
        "status": "active",
        "domain": "shop.example.com",
        "attached_product_key": RETAILER_PK,
        "last_seen_at": NOW - timedelta(days=1),
    }


def identity(**overrides):
    base = {
        "source_listing_ref": "seed:77",
        "identity_status": "approved",
        "identity_confidence": 0.9,
        "live_read_enabled": True,
        "review_required": False,
        "sellable_item_group_id": "sig_example",
    }
    base.update(overrides)
    return base


def inputs(**overrides):
    base = {
        "subject_type": "product",
        "subject_key": RETAILER_PK,
        "product": retailer_product(),
        "identity": None,
        "ips": eligible_ips(),
        "external_seed": active_seed(),
        "pdp_route_resolvable": True,
        "row_has_priced_offer": True,
        "row_is_elected_canonical": None,
        "now": NOW,
    }
    base.update(overrides)
    return base


def derive(**overrides):
    return derive_trust(inputs(**overrides))


def without(row, *keys):
    return {k: v for k, v in row.items() if k not in keys}


# ---------------------------------------------------------------------------
# The exemption itself
# ---------------------------------------------------------------------------


def test_today_a_retailer_listing_with_no_identity_is_shadow():
    """The measured prod state the rule exists for (IDENTITY_CONFIDENCE_NULL)."""
    out = derive()
    assert out["serving_decision"] == "shadow"
    assert REASON_CODES.IDENTITY_CONFIDENCE_NULL in out["serving_reason_codes"]
    assert REASON_CODES.CRAWL_FRESH not in out["serving_reason_codes"]


def test_fresh_listing_with_no_identity_goes_public_with_the_trace_code():
    out = derive(crawl_fresh=True)
    assert out["serving_decision"] == "public"
    assert out["serving_reason_codes"] == [
        REASON_CODES.CRAWL_FRESH,
        REASON_CODES.PUBLIC_PASSTHROUGH,
    ]
    assert out["identity_status"] == "unknown"
    assert out["policy_version"] == POLICY_VERSION


def test_fresh_listing_with_live_read_disabled_goes_public():
    """Cause 2 on prod: minted, approved, live_read_enabled=false."""
    ident = identity(live_read_enabled=False)
    assert derive(identity=ident)["serving_reason_codes"] == [
        REASON_CODES.IDENTITY_LIVE_READ_DISABLED,
    ]
    out = derive(identity=ident, crawl_fresh=True)
    assert out["serving_decision"] == "public"
    assert out["serving_reason_codes"] == [
        REASON_CODES.CRAWL_FRESH,
        REASON_CODES.PUBLIC_PASSTHROUGH,
    ]


def test_fresh_listing_with_approved_but_null_confidence_goes_public():
    ident = identity(identity_confidence=None)
    assert derive(identity=ident)["serving_decision"] == "shadow"
    out = derive(identity=ident, crawl_fresh=True)
    assert out["serving_decision"] == "public"
    assert REASON_CODES.CRAWL_FRESH in out["serving_reason_codes"]
    assert not COVERAGE_CODES & set(out["serving_reason_codes"])


def test_fresh_listing_with_an_unknown_status_and_a_confidence_goes_public():
    """The silent shadow: status neither approved nor review, confidence set ->
    shadow today with NO reason code. The exemption covers it, and the trace
    code is what tells the two apart afterwards."""
    ident = identity(identity_status="pending", identity_confidence=0.4)
    today = derive(identity=ident)
    assert today["serving_decision"] == "shadow"
    assert not COVERAGE_CODES & set(today["serving_reason_codes"])
    out = derive(identity=ident, crawl_fresh=True)
    assert out["serving_decision"] == "public"
    assert out["serving_reason_codes"] == [
        REASON_CODES.CRAWL_FRESH,
        REASON_CODES.PUBLIC_PASSTHROUGH,
    ]


def test_fresh_listing_public_on_its_own_identity_gets_no_new_code():
    """Switching the rule on must not rewrite a row it does not move: the
    UPSERT guard compares reason codes, so a new code here is a write."""
    ident = identity()
    assert derive(identity=ident, crawl_fresh=True) == derive(identity=ident)


def test_the_trace_code_is_in_the_vocabulary():
    assert REASON_CODES.CRAWL_FRESH == "CRAWL_FRESH"
    assert REASON_CODES.CRAWL_FRESH in REASON_CODE_VOCABULARY


# ---------------------------------------------------------------------------
# Stale / absent: today's gates, byte for byte
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [False, None, 0, ""])
def test_stale_or_absent_is_exactly_today(value):
    for ident in (None, identity(live_read_enabled=False), identity(identity_confidence=None)):
        assert derive(identity=ident, crawl_fresh=value) == derive(identity=ident)


def test_an_absent_key_and_an_explicit_none_are_the_same():
    assert derive_trust(without(inputs(), "crawl_fresh")) == derive(crawl_fresh=None)


# ---------------------------------------------------------------------------
# Never exempted
# ---------------------------------------------------------------------------


def test_review_required_still_shadows():
    ident = identity(identity_status="review_required")
    out = derive(identity=ident, crawl_fresh=True)
    assert out["serving_decision"] == "shadow"
    assert out["serving_reason_codes"] == [REASON_CODES.IDENTITY_REVIEW_REQUIRED_LIVE_READ]
    assert out == derive(identity=ident)


def test_approved_but_flagged_for_review_still_shadows():
    ident = identity(review_required=True, live_read_enabled=False)
    out = derive(identity=ident, crawl_fresh=True)
    assert out["serving_decision"] == "shadow"
    assert out == derive(identity=ident)


def test_a_force_review_override_still_shadows():
    override = {"id": "ovr_1", "action_type": "force_review_required", "active": True}
    out = derive(override=override, crawl_fresh=True)
    assert out["serving_decision"] == "shadow"
    assert REASON_CODES.IDENTITY_REVIEW_REQUIRED_LIVE_READ in out["serving_reason_codes"]


def test_identity_conflict_still_blocks():
    ident = identity(identity_status="conflict")
    out = derive(identity=ident, crawl_fresh=True)
    assert out["serving_decision"] == "blocked"
    assert out["serving_reason_codes"] == [REASON_CODES.IDENTITY_CONFLICT]
    assert out == derive(identity=ident)


def test_non_canonical_duplicate_still_shadows():
    out = derive(row_is_elected_canonical=False, crawl_fresh=True)
    assert out["serving_decision"] == "shadow"
    # The coverage gate was lifted (hence the trace code); the duplicate gate
    # was not, and it alone keeps the row out of public.
    assert out["serving_reason_codes"] == [
        REASON_CODES.CRAWL_FRESH,
        REASON_CODES.NON_CANONICAL_DUPLICATE,
    ]


def test_the_elected_canonical_goes_public():
    out = derive(row_is_elected_canonical=True, crawl_fresh=True)
    assert out["serving_decision"] == "public"


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"product": retailer_product(suppression_reason="retired")}, REASON_CODES.ROW_TOMBSTONED),
        ({"external_seed": {**active_seed(), "status": "disabled"}}, REASON_CODES.EXTERNAL_SEED_INACTIVE),
        ({"ips": None}, REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE),
        ({"ips": {**eligible_ips(), "serving_eligible": False}}, REASON_CODES.INDEX_NOT_SERVING_ELIGIBLE),
        ({"product": retailer_product(sync_status="paused")}, REASON_CODES.PUBLISH_STATE_NOT_PUBLIC),
        ({"row_has_priced_offer": False}, REASON_CODES.OFFER_PRICE_MISSING),
        (
            {
                "active_quarantines": [
                    {"state": "active", "match_type": "domain", "match_value": "shop.example.com"}
                ]
            },
            REASON_CODES.SOURCE_QUARANTINED,
        ),
    ],
)
def test_blocked_causes_still_block(overrides, reason):
    today = derive(**overrides)
    out = derive(crawl_fresh=True, **overrides)
    assert out["serving_decision"] == "blocked"
    assert reason in out["serving_reason_codes"]
    assert out == today


def test_a_test_merchant_still_blocks():
    from services.test_merchant_policy import KNOWN_TEST_MERCHANT_IDS

    rig = sorted(KNOWN_TEST_MERCHANT_IDS)[0]
    product = retailer_product(merchant_id=rig)
    out = derive(product=product, crawl_fresh=True)
    assert out["serving_decision"] == "blocked"
    assert out["serving_reason_codes"][-1] == REASON_CODES.TEST_MERCHANT_EXCLUDED
    assert out == derive(product=product)


def test_the_renderable_gate_still_blocks(monkeypatch):
    monkeypatch.setenv("CATALOG_TRUST_RENDERABLE_GATE", "1")
    out = derive(pdp_route_resolvable=False, crawl_fresh=True)
    assert out["serving_decision"] == "blocked"
    assert out["serving_reason_codes"][-1] == REASON_CODES.PDP_ROUTE_UNRESOLVABLE


def test_a_known_unrenderable_pdp_is_never_promoted_even_with_the_gate_off(monkeypatch):
    monkeypatch.delenv("CATALOG_TRUST_RENDERABLE_GATE", raising=False)
    out = derive(pdp_route_resolvable=False, crawl_fresh=True)
    assert out["serving_decision"] == "shadow"
    assert out == derive(pdp_route_resolvable=False)


def test_an_uncomputed_renderability_does_not_hold_the_exemption_back():
    out = derive(pdp_route_resolvable=None, crawl_fresh=True)
    assert out["serving_decision"] == "public"


# ---------------------------------------------------------------------------
# Brand-official / curated drain rows: the same arm, the same code
# ---------------------------------------------------------------------------


def brand_official_product(**overrides):
    """A brand-official drain row: a slug key, a brand seller that is NOT an
    observed seller, external-seed content. Shadow today on coverage."""
    base = retailer_product(
        product_key="ext:serum-slug::0123abcd",
        merchant_id="merch_brand_0123456789ab",
        source_domain="brand.example.com",
        seed_kind="cross",
    )
    base.update(overrides)
    return base


@pytest.mark.parametrize("seed_kind", ["cross", "self", None])
@pytest.mark.parametrize(
    "ident,today_code",
    [
        (None, REASON_CODES.IDENTITY_CONFIDENCE_NULL),
        (identity(live_read_enabled=False), REASON_CODES.IDENTITY_LIVE_READ_DISABLED),
    ],
)
def test_a_fresh_brand_official_row_goes_public_with_the_same_code(seed_kind, ident, today_code):
    product = brand_official_product(seed_kind=seed_kind)
    today = derive(product=product, identity=ident)
    assert today["serving_decision"] == "shadow"
    assert today_code in today["serving_reason_codes"]
    out = derive(product=product, identity=ident, crawl_fresh=True)
    assert out["serving_decision"] == "public"
    assert out["serving_reason_codes"] == [
        REASON_CODES.CRAWL_FRESH,
        REASON_CODES.PUBLIC_PASSTHROUGH,
    ]


def test_a_stale_brand_official_row_is_today():
    product = brand_official_product()
    assert derive(product=product, crawl_fresh=False) == derive(product=product)


# ---------------------------------------------------------------------------
# Other lanes untouched, whatever the input says
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "product",
    [
        # right writer, not a retailer key (a curated / brand-official slug row)
        # another writer's key space
        retailer_product(product_key="prod::external_seed::merch_x::ext_1"),
        # retailer key, another writer
        retailer_product(source_system="external_product_seeds_mirror_v1"),
        retailer_product(source_system=None),
        # the prefix must be a PREFIX
        retailer_product(product_key="x" + RETAILER_PK),
        retailer_product(product_key=RETAILER_PK.upper()),
    ],
)
def test_the_input_alone_never_exempts_another_lane(product):
    for ident in (None, identity(live_read_enabled=False)):
        assert derive(product=product, identity=ident, crawl_fresh=True) == derive(
            product=product, identity=ident
        )


def test_first_party_rows_are_unchanged():
    product = retailer_product(
        product_key="pk_first_party_1",
        merchant_id="merch_first_party_seller_1",
        platform="shopify",
        source_system="shopify",
        seed_kind=None,
    )
    for ident in (None, identity(live_read_enabled=False)):
        out = derive(product=product, identity=ident, external_seed=None, crawl_fresh=True)
        assert out == derive(product=product, identity=ident, external_seed=None)
    assert REASON_CODES.IDENTITY_NOT_APPLICABLE_FIRST_PARTY in derive(
        product=product, external_seed=None, crawl_fresh=True
    )["serving_reason_codes"]


@pytest.mark.parametrize("seed_kind", ["self", None])
def test_observed_seller_self_rows_are_unchanged_even_on_a_retailer_key(seed_kind):
    """Already exempt by the observed-seller tier: they keep that tier's code
    and never get the crawl code."""
    product = retailer_product(seed_kind=seed_kind)
    out = derive(product=product, crawl_fresh=True)
    assert out == derive(product=product)
    assert out["serving_reason_codes"] == [
        REASON_CODES.IDENTITY_NOT_APPLICABLE_FIRST_PARTY,
        REASON_CODES.PUBLIC_PASSTHROUGH,
    ]


def test_a_missing_product_is_never_exempt():
    out = derive_trust(
        {
            "subject_type": "product",
            "subject_key": RETAILER_PK,
            "crawl_fresh": True,
            "now": NOW,
        }
    )
    assert out["serving_decision"] == "shadow"


# ---------------------------------------------------------------------------
# Byte-identical without the input: a wide matrix
# ---------------------------------------------------------------------------


_IDENTITIES = [
    None,
    identity(),
    identity(live_read_enabled=False),
    identity(identity_confidence=None),
    identity(identity_status="review_required"),
    identity(identity_status="conflict"),
    identity(identity_status="pending", identity_confidence=0.3),
    identity(review_required=True),
]
_PRODUCTS = [
    retailer_product(),
    retailer_product(seed_kind="self"),
    retailer_product(product_key="ext:serum-slug::0123abcd", merchant_id="merch_brand_x"),
    retailer_product(product_key="pk_first_party_1", merchant_id="merch_fp", platform="shopify",
                     source_system="shopify", seed_kind=None),
    retailer_product(product_key="ext_4242", merchant_id="external_seed",
                     source_system="external_product_seeds_mirror_v1", seed_kind=None),
    retailer_product(suppression_reason="retired"),
]


@pytest.mark.parametrize(
    "product,ident,elected,route,priced",
    list(itertools.product(_PRODUCTS, _IDENTITIES, [None, True, False], [None, True, False], [True, False])),
)
def test_without_a_true_input_every_decision_is_identical(product, ident, elected, route, priced):
    base = dict(product=product, identity=ident, row_is_elected_canonical=elected,
                pdp_route_resolvable=route, row_has_priced_offer=priced)
    absent = derive_trust(without(inputs(**base), "crawl_fresh"))
    assert derive(crawl_fresh=False, **base) == absent
    assert derive(crawl_fresh=None, **base) == absent
    fresh = derive(crawl_fresh=True, **base)
    # With the input on, only the decision/reason codes may move, and only
    # from shadow to public (or a shadow gaining the trace code).
    assert without(fresh, "serving_decision", "serving_reason_codes") == without(
        absent, "serving_decision", "serving_reason_codes"
    )
    if fresh != absent:
        assert absent["serving_decision"] == "shadow"
        assert REASON_CODES.CRAWL_FRESH in fresh["serving_reason_codes"]
        assert fresh["serving_decision"] in {"shadow", "public"}


# ---------------------------------------------------------------------------
# The two predicates
# ---------------------------------------------------------------------------


def test_listing_predicate_constants():
    assert CRAWL_LISTING_KEY_PREFIX == "ext:"
    assert CRAWL_SOURCE_SYSTEM == "catalog_enrichment_agent_v1"
    assert CRAWL_FRESH_DEFAULT_DAYS == 7


def test_listing_predicate():
    assert is_crawl_listing(retailer_product()) is True
    assert is_crawl_listing(None) is False
    assert is_crawl_listing({}) is False
    assert is_crawl_listing(retailer_product(source_system=" catalog_enrichment_agent_v1")) is False
    assert is_crawl_listing(retailer_product(product_key="ext:retailer")) is True
    assert is_crawl_listing(retailer_product(product_key="ext:serum-slug::0123abcd")) is True
    assert is_crawl_listing(retailer_product(product_key="ext")) is False
    assert is_crawl_listing(retailer_product(product_key="ext_4242")) is False


@pytest.mark.parametrize(
    "age,days,fresh",
    [
        (timedelta(0), 7, True),
        (timedelta(days=7), 7, True),  # inclusive bound
        (timedelta(days=7, seconds=1), 7, False),
        (timedelta(days=3, hours=23), 4, True),
        (timedelta(days=4, seconds=1), 4, False),
        (timedelta(hours=12), 0.5, True),
        (timedelta(hours=12, seconds=1), 0.5, False),
        (timedelta(minutes=-5), 7, True),  # a stamp slightly ahead of `now`
        (timedelta(days=-10), 7, True),  # ahead by more than N: still a read, still fresh
    ],
)
def test_freshness_window(age, days, fresh):
    assert is_crawl_fresh(NOW - age, now=NOW, max_age_days=days) is fresh


def test_freshness_of_nothing_is_false():
    assert is_crawl_fresh(None, now=NOW, max_age_days=7) is False
    assert is_crawl_fresh("not a date", now=NOW, max_age_days=7) is False
    assert is_crawl_fresh(NOW, now=None, max_age_days=7) is False


def test_freshness_reads_iso_strings_and_naive_datetimes_as_utc():
    assert is_crawl_fresh("2026-10-14T12:00:00Z", now=NOW, max_age_days=6) is True
    assert is_crawl_fresh("2026-10-14T11:59:59Z", now=NOW, max_age_days=6) is False
    naive_now = NOW.replace(tzinfo=None)
    assert is_crawl_fresh(naive_now - timedelta(days=1), now=naive_now, max_age_days=1) is True
