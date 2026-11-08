# ADR-029: One payment router: wallets, network tokens and card numbers as declared providers

**Status:** Proposed, draft for review, 2026-11-08. Nothing here is built or switched on by this ADR.

**Decision owner:** peng

**Builds on:**
- **ADR-014** (one spine, protocols as edge adapters). This ADR is the spine's settlement port: the adapter does not pick how a purchase is paid; the router does.
- **ADR-015** (one Canonical Authorized Action, one deny-by-default gate). Every provider's buyer step maps onto it (D5).
- **ADR-016** (Pivota is non-custodial). Unchanged and binding (D3, D4).
- **ADR-012-agent-transaction-rail-boundary** (rail by caller class and settlement; the buyer's consent and its ceiling).
- **ADR-028** (Pivota completes the order on the merchant's checkout). Its D6 (no raw card number in a Pivota system), D1 (a decline is never retried elsewhere) and D7 (the buyer confirms exactly what is paid) bind every provider here.
- **Payment orchestration P0 to P2c** (`services/payment_orchestration/`). It has the unified purchase vocabulary (`rails.py`), the parent ledger (`db/agent_purchase_ledger.py`, executor `rail_managed`) and the router (`router.choose_rail`). This ADR extends that code. It does not start a second one.

## Context

### The ways a buyer can pay, as of 2026-11-08

| Provider | Credential | Who places the order (executor) | Where it can work | Buyer approval | Card data reaching Pivota | State |
|---|---|---|---|---|---|---|
| Human handoff (`/r`, a checkout `continue_url`) | none | the buyer | anywhere | the buyer pays | none | live |
| Reap agentic purchase | the buyer's card, enrolled at Reap | Reap's agent (`rail_managed`) | merchants Reap has proven, per market | Reap's approval page | none | built, off (`REAP_AGENTIC_ENABLED` unset) |
| A2A token rail (a Visa Intelligent Commerce agent enabler) | network token plus Visa passkey | the provider's checkout agent (`rail_managed`) | merchants proven on the rail | Visa passkey per order | none (`vic_order.py` strips credentials) | built to card binding; the order step waits for the partner's spec |
| Shop Pay through Shop MCP | the shopper's Shop Pay wallet | Shopify, for a signed-in shopper | Shopify stores that offer Shop Pay | Shop sign-in, plus whatever Shopify requires per order | none | not built; needs a Token-tier permission from Shopify |
| Shop spend request | a single-use card number | whoever types it into the merchant's checkout | non-Shopify stores that take cards | Shop approval | **the full card number, to the calling agent** | not built |
| Pivota-issued card (`/agent/v1/cards`) | a card funded by Pivota | — | — | — | — | **refused in production** (out of policy under ADR-016) |
| Merchant protocol completion (ADR-028 protocol route) | the protocol's payment handler | Pivota, through the merchant's protocol | merchants that grant completion at a tier we hold | ADR-028 D7 mandate | depends on the handler | not built |

### What is wrong with the current shape

1. **"Rail" does not describe Shop Pay.** Shop Pay is a wallet that the merchant's own checkout accepts, and Shopify completes the order. The orchestrator's vocabulary assumes a rail partner that places the order (`rail_managed`). It has no executor for Shopify and no notion of a credential the buyer already holds.
2. **Card numbers collide with ADR-028 D6.** A spend request hands the agent a card number, expiry and CVC. A Reap virtual card, if one were ever handed to us, would do the same. Neither may enter a Pivota system as ADR-028 is written.
3. **Every door decides for itself.** The 2026-11-08 checkout gate map found eight purchase paths, each with its own gates, and five merchant key spaces that are never joined. The gateway's submit door is on while the backend's `SUBMIT_PAYMENT` is off. That is why the merchant directory, the warm-handoff facts and the UCP door's storefront checkout disagree about the same merchant.
4. **The router reads configuration only.** `choose_rail` picks between two rails from environment lists (`A2A_RAIL_MERCHANTS`, the pilot agents, the per-currency caps). It does not read the purchasability facts the sweep gathers, or which payment methods the buyer already has.

## Decision

### D1. One router decides how every purchase is paid

`services/payment_orchestration` is the only place that decides how a purchase is paid and who places the order. Every door that can start a paid purchase asks it: the UCP door's storefront checkout, the native MCP door and REST `/agent/v2/commerce/purchases`. No door picks a payment path of its own. A door that cannot reach the router hands the purchase off to the buyer; it never decides on its own. The `/r` warm handoff takes no payment and stays a handoff; the router only records it as one (D9).

### D2. Providers declare what they can do; the router never branches on a name

Each provider registers a declaration. The router compares declarations, and an undeclared provider fails closed.

| Field | Values |
|---|---|
| `credential` | `wallet_at_checkout` · `network_token` · `card_number` · `none` |
| `executor` | `shop` · `rail_managed` · `merchant_protocol` · `human` |
| `works_on` | platform; the payment handlers the checkout must advertise; markets; currencies |
| `buyer_step` | how the buyer approves (maps to ADR-015, D5) |
| `settles_to` | who receives the money (D4) |
| `refunds` | the provider's refund and dispute path |
| `limits` | caps per currency; proven merchants |
| `dial` | the provider's own off switch (D8) |

### D3. No credential reaches a Pivota system

ADR-028 D6 stays as written, and every provider is held to it:
- **Allowed:** `wallet_at_checkout` and `network_token` providers whose executor is not Pivota, or whose credential never leaves the executor's systems.
- **A `card_number` provider is allowed only** when the number goes from the issuer straight to an executor that is not Pivota, under that executor's own PCI scope. Pivota sees at most the last four digits and a reference.
- **Shop spend requests are not admitted** as Shopify ships them today, because the number is returned to the calling agent. Q1 asks whether they should come in through a PCI-scoped executor.
- **Pivota-funded cards stay refused** (ADR-016).

### D4. The money always settles to the merchant

A provider is admitted only when its charge settles to the merchant, or to the merchant's own payment provider. A provider whose capture can settle to Pivota, or to a platform default account on the provider's side, is refused at registration. Its admission records the funds flow in writing.

### D5. One authorization, whatever the provider

The ADR-015 Canonical Authorized Action is the only authorization. Each provider's buyer step produces one: the Shop sign-in and approval, the Visa passkey, Reap's approval page, or the buyer paying in person (handoff). The ADR-015 gate checks the executed merchant, currency and amount against it. The ADR-028 D7 confirmation is the cart mandate it binds to. A provider whose buyer step cannot be mapped onto the CAA is not admitted.

### D6. The routing rule, per merchant, market and buyer

**Hard filters.** A provider is a candidate only when all of these hold:
- its declaration covers this merchant, market and currency;
- the purchasability fact allows it. A `NO_CARD_PAYMENT` verdict excludes every card-based credential; `LOGIN_REQUIRED`, `PASSWORD_PAGE` and `NOT_ACCEPTING_ORDERS` exclude every provider except the handoff;
- the merchant is proven for that provider, if the provider requires it;
- the amount is within the provider's cap;
- its dial is on (D8);
- the buyer holds the method, or the provider can enrol it in the flow.

**Ranking among the candidates:**
1. the buyer's explicit choice (today's `payment_preference.rail`), refused rather than redirected when it fails a filter;
2. the lower buyer total (ADR-028 D2);
3. the provider that carries our authorized attribution (ADR-028 D3);
4. fewer buyer steps: a method the buyer already holds before a new enrolment;
5. the default order: `wallet_at_checkout`, then `network_token`, then a `card_number` provider.

**Always:**
- the human handoff is the last candidate and is never filtered out;
- no fallback after a payment decline (ADR-028 D1);
- no failover after the purchase's first write (the router's rule 5 today).

**The starting table** (what the rule produces with today's providers):

| Merchant | Buyer | First choice | Otherwise |
|---|---|---|---|
| Shopify store with Shop Pay | linked Shop account | Shop Pay (`shop`) | handoff |
| Shopify store | no Shop account | token rail, where proven | handoff (prefilled checkout where the fact allows) |
| non-Shopify store accepting the network token | any | token rail, where proven | handoff |
| anything else | any | handoff | — |

### D7. One merchant key

The router, the purchasability facts, the merchant directory, each provider's proven list and every allowlist key a merchant the same way: `(normalize_domain(host), ISO-2 market)`. That is the fact table's own key. Environment lists that hold another spelling are migrated (Phase 1), and the router refuses a list entry it cannot key.

### D8. One kill-switch hierarchy

From the top down:
1. `SUBMIT_PAYMENT` on the backend, for every charge;
2. the provider's dial;
3. the market;
4. the provider's proven-merchant list.

The gateway reads the backend's answer and keeps no switch of its own for the same decision.

### D9. One purchase ledger

The unified purchase (`db/agent_purchase_ledger.py`, `rails.UNIFIED_STATES`) records every purchase, whatever the provider:
- **New executors:** `shop` for Shop Pay. A handoff is recorded as an outcome (`handoff`) so it can be counted.
- **Idempotency:** one key per purchase. A maybe-write is read back, never resent (the rule `vic_order.py` already holds).
- **Every routing answer records the candidates it dropped, and why** (the router's `dropped` today). That is what makes "why did this purchase go there" answerable.

## Phases

1. **Route only, dark.**
   - The router reads the purchasability facts and the one merchant key (D7).
   - The providers are today's two rails.
   - Every door asks it in shadow and logs the answer next to what the door actually did.
   - No payment behaviour changes.
2. **Shop Pay provider**, behind its dial, once Shopify grants completion for signed-in shoppers and the Shop account connect has shipped. One merchant, one market, staff orders first (ADR-028 Phase A).
3. **The token rail's order step**, when the partner's order spec and settlement payee model meet D3 and D4.
4. **Any `card_number` provider**, only after Q1.

## Open dependencies (facts, not decisions)

- Shopify: the Token-tier permission to complete checkouts for signed-in shoppers, and what per-order approval it requires.
- The token-rail partner: its order step, its funds flow (D4) and how a token is revoked.
- Which checkouts advertise which payment handlers, per merchant and market (a probe, like the purchasability sweep).
- Mastercard Agent Pay: not reviewed for this ADR.
- Reap's card-token mode: whether Reap's own agent fills the checkout (`rail_managed`, admitted) or the token is handed to us (D3 refuses it).

## Questions for the owner

1. **Card numbers.** Keep D3 strict, which excludes Shop spend requests unless a PCI-scoped executor receives the number? Or open a separate decision for a card vault, which would change ADR-028 D6 and Pivota's card-data scope?
2. **Price against convenience.** When Shop Pay's total is higher than a token rail's for the same item, does the lower total still win (D6 rank 2)? Or does a method the buyer already holds win?
3. **Shadow routing.** Should every door start asking the router in shadow now (Phase 1), before any new provider exists?

## Consequences

- One answer to "how is this paid, and who places the order" for every door. The disagreements in the gate map become router decisions with recorded reasons.
- New payment programs (Shop Pay, Mastercard Agent Pay, further enablers) arrive as declarations plus an executor client, not as new door logic.
- Card-data scope stays where ADR-028 put it. Some providers are excluded until Q1 is decided.
- The purchasability sweep becomes an input to payment routing, so its coverage limits which providers can be offered.

## Alternatives considered

- **Each door keeps choosing its own path.** Rejected: that is the current state, and the gate map shows the disagreements it produces.
- **Pick one provider for everything.** Rejected: no single provider works on both Shopify and non-Shopify stores, and wallets, tokens and handoffs reach different buyers.
- **Pivota as the payment processor, or holding funds.** Rejected by ADR-016.
- **A Pivota card vault, so spend requests and card numbers could be used directly.** Not taken now: it reverses ADR-028 D6 and brings card numbers into Pivota systems. It is Q1, a separate decision.
