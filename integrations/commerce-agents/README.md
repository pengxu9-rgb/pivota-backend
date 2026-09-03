# Pivota storefront backend for Claude commerce agents

[anthropics/commerce-agents](https://github.com/anthropics/commerce-agents) is Anthropic's
reference for shopping and merchant agents on Claude. Its shopping agent reaches a catalog
through one interface, `StorefrontBackend`. This package implements that interface over
Pivota's MCP tools, so an agent built on the blueprint searches Pivota's multi-merchant
index, reads decision-grade product records, and hands each merchant's cart to a hosted
checkout page.

```text
shopping agent (any of the blueprint's three runtimes)
  -> StorefrontBackend            pivota_storefront.PivotaStorefrontBackend
  -> McpTransport                 pivota_storefront.HttpMcpTransport (JSON-RPC tools/call)
  -> Pivota MCP door              search_catalog, get_product, create_checkout_session,
                                  create_payment_link, get_order
```

## Use

```python
from pathlib import Path

from shopping_agent import ShoppingAgentConfig
from shopping_agent_runtime import ShoppingAgent

from pivota_storefront import HttpMcpTransport, PivotaShoppingSession, PivotaStorefrontBackend

backend = PivotaStorefrontBackend(
    HttpMcpTransport("https://commerce.mcp.pivota.cc/mcp", bearer=DOOR_TOKEN),
    currency="USD",
    merchant_id=None,        # or one merchant id to scope search to a single store
)
agent = ShoppingAgent(
    backend=backend,
    skills_dir=Path("shopping-agent/skills"),
    config=ShoppingAgentConfig(
        brand_name="Your Store",
        enable_policies=False,      # Pivota exposes no policy search
        enable_fulfillment=False,   # delivery options are quoted at checkout, not before
        enable_disclosures=True,    # Pivota Insights as the facts box
    ),
)
session = PivotaShoppingSession(
    session_id=unguessable_id, user_id=principal,
    customer_email=email_from_sign_in,   # what the hosted checkout page needs
    bearer_token=buyer_token,            # a signed-in buyer's token for the door
)
```

The host binds identity at session start, as the blueprint's `docs/backends.md` asks: the
email and the buyer token live on the session, never in a tool argument.

## What maps

| Backend method | Pivota tool | Notes |
|---|---|---|
| `search_products` | `search_catalog` | rows are plain products; `filters.category`, `min_price`, `max_price` pass through |
| `get_product_details` | `get_product` | a record with option-bearing variants is a family; a variant's id returns that variant |
| `get_disclosure` | `get_product` + `include: ["decision"]` | Pivota Insights (why it stands out, best for, evidence profile), attributed to Pivota |
| `get_cart`, `add_to_cart`, `update_cart_item`, `remove_from_cart` | none | held per session in this process; Pivota has no cart |
| `checkout_handoff` | `create_checkout_session`, then `create_payment_link` | one hosted page per merchant in the cart, with the variant the buyer chose |
| `get_order` | `get_order` | |
| `get_orders` | none | Pivota lists no orders for a buyer; returns `[]` |
| `search_policies` | none | returns `[]`; switch `enable_policies` off |
| `get_fulfillment_options` | none | `NotOffered`; switch `enable_fulfillment` off |
| `get_preferences` | none | a guest profile from the session's `user_id` |

## Ids

The blueprint gives a product one id. Pivota addresses a product by merchant and product,
and a variant by a third key, and two merchants can reuse a platform id. So every id the
agent sees carries all of it: `merch_x/9854988910809` for a family or plain product,
`merch_x/9854988910809#4739` for one of its variants. `get_product_details` resolves any
of them statelessly; an id the model invents that does not parse is an unknown product.

## The marketplace rule

`docs/backends.md` in the blueprint: a record has one price, so a product several sellers
carry is returned as the offer you would sell, and a marketplace hands off one checkout
per seller. Pivota's index already resolves the offer before a row leaves `search_catalog`,
and `checkout_handoff` returns one `CheckoutHandoff` per merchant, so both halves hold
without adapter logic.

## Cart rules the backend enforces

The blueprint's executor gates cart writes on provenance and holds a family until a
variant is chosen. The backend enforces the same on its own path, ids only in the message:
a family with options is refused naming its in-stock variants, an out-of-stock variant is
refused naming its in-stock siblings, and an id that is not ours is unknown.

## Idempotency

`checkout_handoff` derives its `idempotency_key` from the session, the merchant, and the
cart lines, so the same cart handed off twice replays the same session and page instead of
opening a second one.

## Run the tests

The blueprint's packages are not on PyPI; install them from its repo:

```bash
git clone https://github.com/anthropics/commerce-agents.git /tmp/commerce-agents
python3.11 -m venv .venv-ca && source .venv-ca/bin/activate
pip install -e /tmp/commerce-agents/commerce-common -e /tmp/commerce-agents/shopping-agent/core \
            pytest pytest-asyncio httpx
cd integrations/commerce-agents && PYTHONPATH=. pytest
```

These tests are not part of the backend's CI sweep (`tests/` and `readiness/tests/`),
because the sweep's environment does not carry the blueprint's packages.

## What is verified, and what is not

- The record shapes the tests script (`search_catalog` rows, `get_product` variants with
  `options`, the hosted URL field precedence `hosted_url`, `checkout_url`, `url`,
  `redirect_url`) are the gateway's, read from its code and a captured `search_catalog`
  response.
- No live `tools/call` was run against a Pivota door while writing this. The
  `create_checkout_session` result's session-id field is read from `session_id`,
  `checkout_session_id`, or `id`, flat or under `checkout_session`; confirm against a live
  session before relying on it.
- `create_checkout_session` requires a signed-in buyer; a guest session gets the host's own
  checkout route (`checkout_handoff` returns `[]` and logs why).
