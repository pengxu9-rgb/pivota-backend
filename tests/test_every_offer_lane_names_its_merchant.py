"""Every lane in `offers.resolve` must name its merchant at the TOP LEVEL of the offer.

WHY THIS IS A CONTRACT AND NOT A STYLE POINT. `offers.resolve` is the one surface on the agent
door that returns a cross-merchant LIST — the retailer arm's SQL has no LIMIT 1 and dedupes on
DESTINATION HOST precisely so a StyleKorean price sits beside the brand's own as two real
sellers (routes/agent_shop_gateway.py, the SHAPE/UNGATED note above the catalog_offers arm).
Measured 2026-09-06: 1,124 products carry an unsuppressed retailer offer and 1,110 are also
seeded, so ~1,110 products genuinely have two sellers today.

But the gateway's `offerToSignal` (PIVOTA-Agent src/agentSignals/offerToSignal.js:90-97) reads
TOP-LEVEL `merchant_id` / `merchant_name` and projects NEITHER `seller` NOR
`internal_checkout_items`. Only the catalog_offers arm emitted those, so offers from the other
two lanes reached the agent as `merchant_id: null` — rows in a comparison list with no seller to
name and nothing to attribute a click to. A cross-merchant list whose rows cannot be named is
not a choice, which is the whole point of the surface.

These tests assert the shape at the SOURCE, per lane, so a fourth lane cannot be added without
answering the question.
"""

from __future__ import annotations

import ast
import pathlib

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_SRC = _ROOT / "routes/agent_shop_gateway.py"

# What the gateway's offerToSignal actually reads off an offer. Transcribed, not inferred.
_GATEWAY_READS = ("merchant_id", "merchant_name")


def _dict_literals_with(key: str):
    """Every dict literal in the module that carries `key` as a string key."""
    tree = ast.parse(_SRC.read_text())
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if key in keys:
                out.append((node, keys))
    return out


def test_every_offer_dict_that_names_a_seller_also_names_the_merchant():
    """`seller` is the field the three lanes agreed on and the gateway ignores. Anywhere one is
    built, the two fields the gateway DOES read must be there too."""
    offers = _dict_literals_with("seller")
    assert offers, "no offer dict carries `seller` any more — has the shape changed wholesale?"
    bad = []
    for node, keys in offers:
        # Only offer-shaped dicts: they carry a price and an offer_id alongside the seller.
        if "offer_id" not in keys or "price" not in keys:
            continue
        missing = [f for f in _GATEWAY_READS if f not in keys]
        if missing:
            bad.append((node.lineno, missing, sorted(keys)[:8]))
    assert not bad, (
        "these offer dicts name a `seller` the gateway never reads, and omit %s which it does:\n"
        "%s\nAn offer that reaches the agent with merchant_id: null is an anonymous row in a "
        "cross-merchant comparison." % (list(_GATEWAY_READS), bad)
    )


def test_all_three_lanes_are_covered():
    """A count, so that deleting a lane's merchant fields and deleting the lane look different.

    Three lanes build an offer: the external-seed lane, the internal-checkout lane
    (`_build_internal_offer_summary`), and the catalog_offers retailer arm."""
    offers = [
        (n, k) for n, k in _dict_literals_with("seller")
        if "offer_id" in k and "price" in k
    ]
    assert len(offers) >= 3, (
        "expected at least three offer-shaped dicts (seed, internal_checkout, catalog_offers); "
        "found %d at lines %s. If a lane was removed, say so here." % (
            len(offers), [n.lineno for n, _ in offers])
    )


def test_the_seed_lane_uses_the_destination_HOST_as_the_merchant_id():
    """Not `external_seed`. The gateway substitutes a host label for that id anyway
    (src/server.js:1998), so using it would collapse every seed to ONE merchant and destroy the
    comparison. The host is also what the catalog_offers arm dedupes on, so the two lanes agree
    on what a merchant is."""
    src = _SRC.read_text()
    assert "seed_merchant_id = (" in src, "the seed lane no longer derives a merchant id"
    start = src.index("seed_merchant_id = (")
    block = src[start:start + 400]
    assert "domain" in block, "the seed merchant id is no longer derived from the domain/host"
    assert '"external_seed"' not in block and "'external_seed'" not in block, (
        "the seed lane sets merchant_id to the literal `external_seed`; every seed offer would "
        "then share one merchant id and the cross-merchant list would look like one seller."
    )
