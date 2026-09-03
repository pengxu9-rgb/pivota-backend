"""The blueprint's shipped executor over this backend. Nothing here is a stand-in
for the executor: the provenance gate, the options hold, the fence, and the
checkout enrichment are the blueprint's own code, driven end to end. What it
proves: the ids this backend hands out survive the gates, a family is held and
its variant taken, and the hosted checkout URL reaches the host's payload and
never the model's text."""

from __future__ import annotations

from typing import Any

from commerce_common.memory import InMemoryMemoryStore
from commerce_common.skills import SkillRegistry
from shopping_agent import ShoppingAgentConfig
from shopping_agent.executor import ShoppingToolExecutor, build_memory
from shopping_agent.fencing import STOREFRONT_FENCE
from shopping_agent.gates import OPTIONS_GATE, PROVENANCE_GATE
from shopping_agent.types import ShoppingSessionState

from pivota_storefront import PivotaShoppingSession, PivotaStorefrontBackend

from test_pivota_storefront import FAMILY_RECORD, MERCHANT, SEARCH_ROW, ScriptedTransport

HOSTED = "https://checkout.stripe.com/c/pay/cs_live_1"


def build(script: dict[str, list[Any]], *, config: ShoppingAgentConfig | None = None):
    transport = ScriptedTransport(script)
    backend = PivotaStorefrontBackend(transport)
    session = PivotaShoppingSession(
        session_id="sess-9", user_id="buyer-9", customer_email="buyer@example.com", bearer_token="tok"
    )
    state = ShoppingSessionState()
    cfg = config or ShoppingAgentConfig(enable_policies=False, enable_fulfillment=False)
    executor = ShoppingToolExecutor(
        backend=backend,
        config=cfg,
        skills=SkillRegistry([]),
        session=session,
        state=state,
        memory=build_memory(cfg, InMemoryMemoryStore()),
    )
    return transport, executor, state


async def test_search_details_cart_and_checkout_through_the_shipped_executor():
    family_id = f"{MERCHANT}/9854988910809"
    transport, executor, state = build(
        {
            "search_catalog": [{"products": [SEARCH_ROW, {**FAMILY_RECORD, "variants": None}]}],
            "get_product": [FAMILY_RECORD],
            "create_checkout_session": [{"checkout_session_id": "cs_live_1"}],
            "create_payment_link": [{"checkout_url": HOSTED}],
        }
    )

    # Search: fenced for the model, remembered as provenance under our ids.
    found = await executor.execute("search_products", {"query": "trail tee"})
    assert not found.is_error
    assert STOREFRONT_FENCE.open in found.result_text
    assert family_id in state.seen_products
    assert "merch_obs_e644ed0256549e83/sig_615cde705e4be2eaf7eea5f25b391728" in state.seen_products

    # An id the model invents is refused by the blueprint's provenance gate, before the backend.
    invented = await executor.execute("add_to_cart", {"product_id": f"{MERCHANT}/000", "quantity": 1})
    assert invented.blocked == PROVENANCE_GATE

    # Details: the family and its variants enter provenance.
    details = await executor.execute("get_product_details", {"product_id": family_id})
    assert not details.is_error
    assert state.seen_products[family_id].has_options
    assert f"{family_id}#v3" in state.seen_products

    # The family is held by the options gate and the model is pointed at its variants.
    held = await executor.execute("add_to_cart", {"product_id": family_id, "quantity": 1})
    assert held.blocked == OPTIONS_GATE
    assert "size" in held.result_text

    # The variant goes in; the out-of-stock sibling is relayed as unavailable, nothing written.
    added = await executor.execute("add_to_cart", {"product_id": f"{family_id}#v3", "quantity": 2})
    assert not added.is_error and added.blocked is None
    assert any(e.type == "cart_update" for e in added.events)
    sold_out = await executor.execute("add_to_cart", {"product_id": f"{family_id}#v2", "quantity": 1})
    assert f"{family_id}#v1" in sold_out.result_text  # the in-stock sibling, by id
    cart = await executor.execute("get_cart", {})
    assert "#v3" in cart.result_text and "#v2" not in cart.result_text

    # Checkout: the hosted URL reaches the host's ui payload and never the model's text.
    checkout = await executor.execute("checkout", {"note": "gift"})
    assert not checkout.is_error, checkout.result_text
    ui = [e for e in checkout.events if e.type == "ui"]
    assert len(ui) == 1
    payload = ui[0].data["payload"]
    assert payload["handoffs"] == [{"url": HOSTED, "label": "Pay on the merchant's page", "seller": MERCHANT}]
    assert HOSTED not in checkout.result_text
    assert "cs_live_1" not in checkout.result_text

    # What left for Pivota: the chosen variant, the quantity, the buyer's token.
    created = next(a for (n, a, _) in transport.calls if n == "create_checkout_session")
    assert created["quote"]["items"] == [{"product_id": "9854988910809", "quantity": 2, "variant_id": "v3"}]
    assert all(b == "tok" for (n, _, b) in transport.calls if n.startswith("create_"))


async def test_disclosure_renders_pivota_insights_through_present_disclosure():
    family_id = f"{MERCHANT}/9854988910809"
    record = {**FAMILY_RECORD, "decision": {"why_it_stands_out": ["Flat seams"], "evidence_profile": "grounded_verified"}}
    transport, executor, state = build(
        {"get_product": [record, record]},
        config=ShoppingAgentConfig(enable_policies=False, enable_fulfillment=False, enable_disclosures=True),
    )
    # Unseen: the blueprint's provenance gate refuses before any call reaches Pivota.
    refused = await executor.execute("present_disclosure", {"product_id": family_id})
    assert refused.blocked == PROVENANCE_GATE and transport.calls == []

    await executor.execute("get_product_details", {"product_id": family_id})
    shown = await executor.execute("present_disclosure", {"product_id": family_id})
    assert not shown.is_error, shown.result_text
    payload = next(e for e in shown.events if e.type == "ui").data["payload"]
    assert payload["title"] == "Pivota Insights"
    assert [(r["label"], r["value"]) for r in payload["rows"]] == [
        ("Why it stands out", "Flat seams"),
        ("Evidence profile", "grounded_verified"),
    ]
