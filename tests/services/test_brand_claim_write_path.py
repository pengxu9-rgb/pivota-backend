"""P1 — brand claim write-path keystone (pure, no DB/DNS).

Proves the one missing primitive end to end at the logic boundary: the value the
claim flow writes (brand_relationship='brand_direct') is exactly what
classify_offer_type needs to surface a brand-direct offer.
"""

from services.brand_claim_service import (
    BRAND_DIRECT,
    brand_direct_metadata,
    dns_txt_proves_claim,
    make_challenge_token,
)
from services.offer_classification import (
    OFFER_TYPE_BRAND_DIRECT,
    classify_offer_type,
)


def test_brand_direct_metadata_sets_and_preserves():
    md = brand_direct_metadata({"existing_key": 1})
    assert md["brand_relationship"] == BRAND_DIRECT
    assert md["existing_key"] == 1  # doesn't clobber other metadata
    # None input is tolerated
    assert brand_direct_metadata(None)["brand_relationship"] == BRAND_DIRECT


def test_keystone_writer_satisfies_reader():
    # The whole point of P1: the metadata the claim flow writes makes
    # classify_offer_type return brand_direct for an internal merchant.
    md = brand_direct_metadata(None)
    assert (
        classify_offer_type("internal_merchant", md["brand_relationship"])
        == OFFER_TYPE_BRAND_DIRECT
    )


def test_unclaimed_internal_merchant_is_not_brand_direct():
    # Before the claim writes brand_relationship, the reader must NOT assume it.
    assert classify_offer_type("internal_merchant", None) is None
    assert classify_offer_type("internal_merchant", "reseller") is None


def test_dns_txt_proves_claim_exact_match_only():
    tok = make_challenge_token()
    assert tok.startswith("pivota-verify=")
    assert dns_txt_proves_claim(tok, ["v=spf1 ...", tok, "google-site-verification=x"])
    assert not dns_txt_proves_claim(tok, ["v=spf1 ...", "pivota-verify=someoneelse"])
    assert not dns_txt_proves_claim(tok, [])
    assert not dns_txt_proves_claim("", [tok])  # empty expected never proves
