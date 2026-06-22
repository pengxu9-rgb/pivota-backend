"""P1 — brand claim write-path keystone (pure, no DB/DNS).

Proves the one missing primitive end to end at the logic boundary: the value the
claim flow writes (brand_relationship='brand_direct') is exactly what
classify_offer_type needs to surface a brand-direct offer.
"""

from services.brand_claim_service import (
    BRAND_DIRECT,
    brand_direct_metadata,
    dns_txt_proves_claim,
    host_matches_known,
    is_valid_public_hostname,
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


# --- B1 brand-identity binding + B4 hostname validation (pure) ---

def test_host_matches_known_binds_to_merchant_domains():
    # exact + same registrable org (subdomain) match
    assert host_matches_known("anua.com", {"anua.com"})
    assert host_matches_known("shop.anua.com", {"anua.com"})
    assert host_matches_known("anua.com", {"shop.anua.com"})
    # an UNRELATED domain the attacker happens to control must NOT bind
    assert not host_matches_known("evil.com", {"anua.com"})
    assert not host_matches_known("anua.com", {"oliveyoung.com", "amazon.com"})
    assert not host_matches_known("anua.com", set())
    # tolerant of URL/scheme/www forms
    assert host_matches_known("https://www.anua.com/products/x", {"anua.com"})


def test_host_matches_known_rejects_public_suffix_widening():
    # B1 hardening: a junk/short host in the merchant's OWN catalog
    # (source_domain/canonical_url) or onboarding (store_url/website) must NOT
    # widen the bind to every org that merely shares a public/platform suffix.
    #
    # bare TLD known host: a stray "com"/"uk" doesn't bind an unrelated domain.
    assert not host_matches_known("anything.com", {"com"})
    assert not host_matches_known("brand.co.uk", {"uk"})
    # shared storefront-platform suffix: one tenant is not another tenant.
    assert not host_matches_known("anybrand.myshopify.com", {"myshopify.com"})
    assert not host_matches_known("anybrand.shopify.com", {"shopify.com"})
    # multi-label registry suffix (2 labels, but still a public suffix).
    assert not host_matches_known("evilbrand.co.uk", {"co.uk"})
    # the reverse direction (claimed domain is the public suffix) is guarded too.
    assert not host_matches_known("co.uk", {"brand.co.uk"})
    assert not host_matches_known("myshopify.com", {"shop.myshopify.com"})

    # ...but legitimate exact + true-subdomain binds STILL pass, including a real
    # registrable domain that merely sits one label under a public suffix.
    assert host_matches_known("anua.com", {"anua.com"})             # exact
    assert host_matches_known("shop.anua.com", {"anua.com"})        # true subdomain
    assert host_matches_known("anua.com", {"shop.anua.com"})        # reverse subdomain
    assert host_matches_known("shop.brand.co.uk", {"brand.co.uk"})  # registrable under co.uk


def test_host_matches_known_rejects_longtail_platform_suffixes():
    # Completeness follow-up to the guard above: the long-tail multi-tenant
    # platform / hosting suffixes in _PUBLIC_SUFFIXES must also refuse to anchor
    # a suffix bind, so one tenant's host can't bind another tenant's domain.
    assert not host_matches_known("evil.github.io", {"github.io"})
    assert not host_matches_known("shop.bigcommerce.com", {"bigcommerce.com"})

    longtail = {
        "github.io", "webflow.io", "editorx.io",
        "bigcommerce.com", "mybigcommerce.com", "wordpress.com",
        "weebly.com", "storenvy.com", "ecwid.com", "shopifypreview.com",
    }
    for suffix in longtail:
        # forward: a tenant subdomain must not bind the bare platform suffix
        assert not host_matches_known("anytenant." + suffix, {suffix}), suffix
        # reverse: the bare platform suffix must not bind a tenant subdomain
        assert not host_matches_known(suffix, {"anytenant." + suffix}), suffix

    # ...while a real registrable org still binds: exact, true subdomain, and two
    # hosts under the SAME tenant (the tenant label — not the platform suffix —
    # is the registrable base, so the guard stays narrow).
    assert host_matches_known("anua.com", {"anua.com"})              # exact still binds
    assert host_matches_known("shop.anua.com", {"anua.com"})         # true subdomain
    assert host_matches_known("cart.brand.myshopify.com", {"brand.myshopify.com"})


def test_is_valid_public_hostname():
    assert is_valid_public_hostname("anua.com")
    assert is_valid_public_hostname("brand.co.kr")
    assert is_valid_public_hostname("https://www.anua.com/x")  # normalized first
    assert not is_valid_public_hostname("localhost")  # no TLD
    assert not is_valid_public_hostname("10.0.0.1")  # IP, not a hostname
    assert not is_valid_public_hostname("not a domain")
    assert not is_valid_public_hostname("")
    assert not is_valid_public_hostname(None)
