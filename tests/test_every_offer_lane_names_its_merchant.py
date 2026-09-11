"""Every lane in `offers.resolve` must name its merchant at the TOP LEVEL of the offer — and
the name it gives must be one the door can still answer questions about.

WHY THIS IS A CONTRACT. `offers.resolve` is the one surface on the agent door that returns a
cross-merchant LIST: the retailer arm has no LIMIT 1 and dedupes on DESTINATION HOST so a
StyleKorean price sits beside the brand's own as two real sellers. Measured 2026-09-06, ~1,110
products have two genuine sellers today. But the gateway's `offerToSignal`
(PIVOTA-Agent src/agentSignals/offerToSignal.js:90-97) reads TOP-LEVEL `merchant_id` /
`merchant_name` and projects neither `seller` nor `internal_checkout_items`, so two of the three
lanes reached an agent as `merchant_id: null`. A list whose rows cannot be named is not a choice.

⚠️ THE FIRST VERSION OF THIS FILE WAS THREE AST SHAPE TESTS, AND THEY DID NOT WORK. Review
mutated both lanes back to `"merchant_id": None` — the exact defect this file exists to prevent —
and all three passed, because they asserted that the KEY was present and that an assignment
STATEMENT existed, never that a value reached an offer. The dead `seed_merchant_id = (...)` line
satisfied the grep. These tests call the code instead.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


@pytest.fixture(scope="module")
def gw():
    import routes.agent_shop_gateway as mod

    return mod


# --- the internal-checkout lane: call it, and read the value off the result ----------------


def test_internal_checkout_offer_carries_the_merchant_that_will_be_transacted_with(gw):
    out = gw._build_internal_offer_summary(
        merchant_id="merch_obs_abc123",
        platform="shopify",
        product_payload={"id": "p1", "merchant_name": "Acme Beauty", "price": 20.0},
        variant_payload={"id": "v1", "price": 20.0},
        confidence=0.9,
        canonical_ref=None,
        canonical_group_id=None,
    )
    assert out["merchant_id"] == "merch_obs_abc123", (
        "the internal-checkout lane does not put the merchant at the top level, so the gateway's "
        "offerToSignal sends the agent merchant_id: null. Got %r" % out.get("merchant_id")
    )
    assert out["merchant_name"] == "Acme Beauty", out.get("merchant_name")
    # It must agree with the id the buyer would actually transact against.
    assert out["internal_checkout_items"][0]["merchant_id"] == out["merchant_id"]


# --- the scope guard: the id we advertise must survive being echoed back -------------------


@pytest.mark.parametrize(
    "advertised",
    ["rovectin.com", "stylekorean.com", "shop.example.co.uk", "external_seed", "EXTERNAL SEED"],
)
def test_an_advertised_seed_merchant_id_echoed_back_does_not_scope_to_nothing(gw, advertised):
    """THE REGRESSION THIS PR NEARLY SHIPPED.

    Agents echo advertised fields back: `get_offers(merchant_id=...)` takes the id we just
    handed them. `catalog_products.merchant_id` is always `merch_obs_…`, never a host, so
    scoping to a host matches zero rows and the follow-up call returns an empty list — having
    just been told that merchant sells the thing. `_offers_scope_or_none` neutralised only the
    literal `external_seed`; a host sailed through."""
    assert gw._offers_scope_or_none(advertised) is None, (
        "%r was accepted as a merchant scope. It is not a Pivota merchant id, so every query "
        "scoped to it matches nothing and the agent's obvious follow-up silently returns "
        "an empty list." % advertised
    )


@pytest.mark.parametrize("real", ["merch_obs_abc123", "merch_123", "acme"])
def test_a_real_merchant_id_is_still_a_usable_scope(gw, real):
    """The control. A guard that rejects everything would also pass the test above."""
    assert gw._offers_scope_or_none(real) == real


# --- the seed lane's id: derived the same way the LINK is ----------------------------------


def test_the_seed_merchant_id_is_normalised_the_way_merchant_domain_is(gw):
    """A seed's merchant is the host the buyer LANDS on, so it must be normalised identically
    to `execution_spec.merchant_domain` — otherwise one offer advertises
    `merchant_id: "https://x.com/"` beside `merchant_domain: "x.com"`."""
    from services.outbound_links_service import normalize_shop_host

    for raw in ("https://Rovectin.com/", "rovectin.com:443", "user@rovectin.com", "rovectin.com."):
        assert normalize_shop_host(raw) == "rovectin.com", raw

    src = (_ROOT / "routes/agent_shop_gateway.py").read_text()
    start = src.index("seed_merchant_id = (")
    block = src[start:start + 320]
    assert "normalize_shop_host(" in block, (
        "the seed lane derives its merchant id without normalize_shop_host, so a `domain` column "
        "holding a URL or a port ships an id that disagrees with the link beside it"
    )
    assert "canonical_url or destination_url" in block, (
        "the fallback reads the RAW destination_url. The link uses `canonical_url or "
        "destination_url`; reading a different input here names a host the buyer never visits — "
        "which the two comments above that block warn about in those words."
    )
    assert '"external_seed"' not in block and "'external_seed'" not in block, (
        "the seed lane sets merchant_id to the sourcing sentinel; every seed offer would share "
        "one merchant id and the cross-merchant list would look like a single seller."
    )


# --- and the shape, still, so a NEW lane cannot skip the question --------------------------


def test_all_three_offer_lanes_name_a_merchant(gw):
    """Exactly three lanes build an offer. `>= 3` could not notice one being deleted while a
    fourth was added, so this is exact and the lines are named."""
    import ast

    tree = ast.parse((_ROOT / "routes/agent_shop_gateway.py").read_text())
    offers = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if {"offer_id", "price", "seller"} <= set(keys):
                offers.append((node.lineno, keys))
    assert len(offers) == 3, (
        "expected exactly 3 offer-shaped dicts (external seed, internal checkout, catalog_offers); "
        "found %d at lines %s. A new lane must answer the merchant-identity question too." % (
            len(offers), [ln for ln, _ in offers])
    )
    for lineno, keys in offers:
        missing = [f for f in ("merchant_id", "merchant_name") if f not in keys]
        assert not missing, (
            "the offer dict at line %d omits %s, which is what the gateway reads" % (lineno, missing)
        )
