# ADR-029: One purchase router for agent-paid orders on merchants not connected to Pivota

**Status:** Proposed, draft for review, 2026-11-08 (rev 3, after an adversarial review and a delta review). Nothing here is built or switched on by this ADR.

**Decision owner:** peng. **Runtime owner of the router:** the payment orchestration code owner (to be named at acceptance).

**Builds on.** All of these are Proposed (SIGNOFF.md is unsigned); this ADR assumes them as written and names where it amends one.
- **ADR-012-agent-transaction-rail-boundary:** the path is chosen by caller class × settlement, not freely; the buyer's consent and its ceiling.
- **ADR-014:** one spine, protocols as adapters. **Amended by D1** for merchants not connected to Pivota (see there).
- **ADR-015:** the Canonical Authorized Action (CAA) and its gate.
- **ADR-016:** Pivota is non-custodial.
- **ADR-028:** Pivota completes the order on the merchant's checkout, D1 to D9. Unchanged, except where D6 below says how its rules are applied before any quote exists. Its D2 would be amended only if question 3 is answered yes.
- **Payment orchestration P0 to P2c** (`services/payment_orchestration/`): the router (`router.choose_rail`), the unified purchase states (`rails.py`) and the parent ledger (`db/agent_purchase_ledger.py`).

## Scope

**Connected, defined.** A seller is **connected** when it has a live `merchant_stores` row (`active` or `connected`) and a payment provider through which Pivota's own order flow charges. The test is on the **seller of the offer being bought**, however the offer was reached: a connected brand's product found through an external seed is still a connected purchase.

**In scope:** a purchase that an agent asks Pivota to have paid, where the seller is **not** connected. That is the population of ADR-028 and of the two existing rails.

**Out of scope, unchanged:** connected sellers paying through their own payment provider. That covers the hosted payment link, `order_routes`, `payment_execution_routes` and the agent payment SDK, which use the existing processor router (`services/payment_routing_service.py`). That router selects a processor per agent, optionally per merchant. Its own retry and failover behaviour is outside this ADR.

**One spine, two settlement selectors (ADR-014).** These invariants stay in the core and are enforced once for both populations: the CAA gate, attribution, receipts and reconciliation. The kill switches are per population (D8). Phase 1 adds one stop above both that halts every agent payment. Only the settlement selection differs:
- for connected sellers, the adapter's `select_settlement` and the processor router;
- for unconnected sellers, this router, which chooses who places the order and with what credential.

Also out of scope: the AP2 and ACP protocol sessions of ADR-012. This ADR does not change those rails. A future adapter that pays an unconnected seller goes through this router.

## Context

### The ways an agent-paid order can be paid on an unconnected merchant

| Path | Credential | Who places the order | Where it can work today | Buyer approval | State |
|---|---|---|---|---|---|
| Reap agentic purchase | the buyer's card, held by Reap | Reap's checkout agent | merchants on our Reap eligibility rows, per market | Reap's approval page | built, dark |
| A2A token rail (a card-network agent enabler) | network token, Visa passkey | the provider's checkout agent | **Shopify stores with Tier B cart-link eligibility** (the opener needs a Shopify cart permalink) | passkey per order | built, dark; order step pending |
| Shop MCP checkout completion (Shop Pay) | the shopper's Shop Pay wallet | **Pivota**, calling Shop MCP `complete_checkout` with the shopper's token (a platform protocol route; see question 3) | Shopify stores offering Shop Pay, signed-in shopper, once Shopify grants the Token-tier permission | Shop sign-in, plus Shopify's per-order rule (not yet known) | not built |
| Merchant protocol completion (UCP `complete_checkout`) | the protocol's payment handler | Pivota (ADR-028 protocol route) | merchants that grant completion at a tier we hold | ADR-028 D7 mandate | not built |
| Pivota's own browser route (ADR-028 D2) | a network token in memory for one attempt (ADR-028 D6) | Pivota | merchants meeting ADR-028 D5's run condition | ADR-028 D7 mandate | not built |
| Shop spend request | a single-use card number **returned to the calling agent** | whoever types it in | non-Shopify stores that take cards | Shop approval | not built |
| Pivota-issued card (`/agent/v1/cards`, via Reap) | a card funded by Pivota | — | — | — | refused in production (ADR-016) |
| Human handoff (`/r`, a checkout `continue_url`) | none | the buyer | anywhere we serve an offer | the buyer pays | live; not a purchase in Pivota's ledger |

### What is wrong with the current shape

1. **Paths are chosen in several places.** Tracing the paid-purchase doors through the code shows that each door checks its own conditions, and the merchant identities they use are not joined. One example: the gateway's submit switch and the backend's protocol-charge switch can disagree, and nothing reconciles them.
2. **The router decides between two rails from configuration only.** The A2A opener applies the purchasability fact, the disabled row and Tier B after routing. So the router cannot say in advance why a purchase will be refused.
3. **The vocabulary has no place for a wallet** or for a route Pivota itself executes. The ledger allows only executor `rail_managed` and rail `reap|a2a`.
4. **A purchase routed to Reap loses its routing record.** The router's Reap branch calls the same `start_reap_purchase` as the direct Reap route, which writes the parent with the default `basis="agent_selected_rail"` and no dropped candidates (`ensure_reap_parent`). A purchase the router chose is therefore labelled as the agent's choice.
5. **The fact check is tied to a switch.** The openers' `is_purchasable` check runs under `MERCHANT_PURCHASABILITY_ENFORCE`.

## Decision

### D1. One purchase router for this scope

Every door that starts an agent-paid purchase on an unconnected seller asks `services/payment_orchestration`. That covers `/agent/v2/commerce/purchases`, the direct Reap route, the UCP door's and native MCP door's completion, and any future adapter.
- **The direct Reap route** (`routes/agent_commerce_reap.py`) keeps its URL, but it runs the router's filters (D6) and switches (D8) for the Reap path before opening. **That changes the route's behaviour:**
  - it needs a fresh fact whatever `MERCHANT_PURCHASABILITY_ENFORCE` says;
  - it applies ADR-028's MUSTs;
  - it needs `PAYMENT_ORCHESTRATOR_ENABLED`;
  - an unset pilot scope admits nobody (D8).

  A request that opens today without these is refused. Its parent keeps `basis="agent_selected_rail"`, which is correct for a direct call.
- **The router's own choices** pass their basis and dropped candidates through to the parent (D9). They are no longer written with the direct route's default.
- **ADR-014 amendment.** For unconnected sellers, this router is the settlement selector; the adapter's `select_settlement` does not choose for them (see "One spine, two settlement selectors" above).

### D2. Paths are declared; the router reads declarations

A path is admitted only with a declaration. An undeclared or incomplete one fails closed.

| Field | Values and meaning |
|---|---|
| `credential` | `wallet_at_checkout` (Shop Pay) · `card_held_by_rail` (Reap) · `network_token` · `protocol_handler` (the merchant protocol's own payment handler) · `card_number` · `none` |
| `executor` | `rail_managed` (a partner's checkout agent) · `merchant_protocol` (Pivota calls the merchant's protocol; a platform protocol such as Shop MCP only if question 3 is answered yes) · `pivota_browser` · `human` |
| `caller_classes` | which callers may use it (ADR-012) |
| `works_on` | platform, required item source (e.g. `cart_link`), the payment handler the checkout must offer, markets, currencies the credential step can carry |
| `requires` | per-merchant facts it needs (D6), pilot scope, worker health, offer-code support |
| `buyer_step` | how the buyer approves, and how it maps to the CAA (D5) |
| `funds_flow` | the written funds flow (D4) |
| `refunds` | who refunds and how a dispute is evidenced (ADR-028 D9) |
| `attribution` | whether and how our click id reaches the order (ADR-028 D3) |
| `limits`, `dial` | caps per currency; the path's own off switch |

The router branches on these fields, never on a path's name. An agent-set preference (today `payment_preference.rail`) is a **filter**: it can narrow the candidates, and is refused when it cannot be met. It never widens them, and it is not treated as the buyer's choice (D6).

### D3. Card data

ADR-028 D6 holds as written. In particular, no raw card number enters a Pivota system, and a token or cryptogram may exist only in memory for one attempt, on a route Pivota executes. On top of that, this ADR requires:
- **A `card_number` path is admitted only when the number goes from the issuer to an executor that is not Pivota,** under that executor's PCI scope. Shop spend requests, which return the number to the calling agent, are therefore not admitted as shipped.
- **Responses that can carry a credential are stripped on arrival,** before any other code reads them, as `vic_order.py` and the Reap issuer adapter already do. "No card data reaches Pivota" in this ADR means stripped on arrival, and never stored, logged or traced.
- **No stored bearer capability to a card.** A reveal handle, or anything that can be exchanged for a card number, is not persisted in production. `/agent/v1/cards` stays refused there.
- **Checkout identifiers that carry access keys** (e.g. a Shop checkout id or `continue_url`) are secrets: never logged, never sent to telemetry, and stored only where a read-back needs them, encrypted at rest. They are not payment credentials, and ADR-028 D6's rule that tokens are never stored is unchanged.

### D4. The money settles to the merchant

A path is admitted only with a written funds flow showing that the buyer's payment settles to the merchant, or to the merchant's own processor, and never to Pivota. Per purchase, the merchant of record and the merchant order number come from the executor's status and are checked against the order Pivota sent. A mismatch is an incident.

### D5. Authorization: before, where Pivota executes; before and after, where a partner does

- **`merchant_protocol` and `pivota_browser`:** the ADR-015 gate runs before payment, deny by default, and consumes the CAA atomically. The ADR-028 D7 confirmation is the cart mandate.
- **`rail_managed`** follows the partner's sequence, which quotes and asks the buyer to authorize only after Pivota submits:
  1. **Before submitting:** Pivota checks the merchant, the currency and the consent's ceiling. The exact total does not exist yet. The CAA is consumed here (single use).
  2. **Before the buyer's authorization step, the authorized amount must be known to Pivota and must match what the buyer confirmed (ADR-028 D7) and be within the ceiling.** That can happen in two ways:
     - a path that quotes after the submit (Reap) satisfies it at the quote: Pivota reads the quoted total and stops the purchase before the buyer's authorization if it does not match;
     - a path whose submitted order fixes the amount the buyer then authorizes (a token minted for exactly the submitted amount) satisfies it at the submit.

     A path that does neither is not admitted.
  3. **After completion:** Pivota reconciles the final amount and the merchant against the CAA. An over-capture is an incident (ADR-028 D7).
  - The partner's buyer step (passkey, approval page) is a **partner attestation**. It is recorded as such, not as an authorization primitive Pivota verified (ADR-015). Whether that level is acceptable for a path is part of its admission.
- **A path whose credential step carries no currency** is admitted for one declared currency only.
- **`human`:** no CAA. The buyer pays the merchant directly.

### D6. Choosing a path

**Hard filters are allow-lists that fail closed.** A path is a candidate only when all of these hold:
- **The fact, by credential:**
  - `card_held_by_rail`, `network_token` or `card_number` need `is_purchasable(host, market)`: a fresh ELIGIBLE fact with a card offered, from the buyer vantage.
  - `wallet_at_checkout` needs a fresh fact that the checkout offers that wallet. That fact does not exist yet; it is an open dependency.
  - A missing, stale or unverifiable fact excludes every agent-paid path.
  - **This filter holds whatever `MERCHANT_PURCHASABILITY_ENFORCE` says.** For paths in this scope the router does not consult that switch, including on the direct Reap route (D1). It keeps its meaning for the consumers outside this scope.
  - The wallet fact and the payment-handler fact do not exist yet. Until they do, Shop MCP completion and merchant protocol completion are never candidates.
- **ADR-028's MUSTs:**
  - the merchant has not opted out (D5);
  - for `pivota_browser`, D5's run condition;
  - for `rail_managed`, the partner's contractual equivalents of D5 to D7 are recorded (D2). A merchant being proven on a rail is necessary, not sufficient: it must also be **permitted**.
- **The caller and the buyer:** the caller class is allowed (ADR-012); the amount is within the consent's ceiling (ADR-028 D7) and the path's cap; the buyer holds the credential or can enrol it in the flow.
- **The path's own requirements** (D2 `requires`, `works_on`) and its dial (D8).

**Order among the candidates** keeps ADR-028 D2's order and only says how it is applied before any quote exists:
1. **A choice the buyer makes** among the paths shown to them, recorded in the consent, wins.
2. **The lower buyer total** (ADR-028 D2 rule 1) wins wherever totals are known **without creating a checkout**. Quoting several paths to compare them is not allowed: it leaves abandoned checkouts, and Shopify asks that Shop checkouts be created only when the shopper asks.
3. **Where totals are not known:** a path that carries our attribution first; when both do, a protocol route before a partner's (ADR-028 D2 rules 2 and 3); then fewer buyer steps.

Attribution never outranks a known lower total.

**Fallback.** This amends the router's rule 4 ("no fallback after routing") to match ADR-028 D1:
- A purchase never changes path after its first write (rule 5 stands).
- When a path **definitely refuses before payment**, the next candidate may be tried as a **new purchase**, with its own idempotency key and a new buyer confirmation (ADR-028 D7).
- An `outcome_unknown` answer is never a refusal. It is read back until it is definite, and no new purchase starts meanwhile.
- On `rail_managed`, "before payment" means before the buyer's authorization step on the partner's page. After that step, a failure stops and returns to the buyer.
- A payment decline stops (ADR-028 D1).

**The handoff** is offered whenever an offer is served, as today. A prefilled cart in a handoff still needs `human_handoff_allowed`. The directory's and search's exclusions (quarantine, test merchants, rejected merchants) apply before anything is served.

What this gives with today's paths:

| Merchant | First candidate | Otherwise |
|---|---|---|
| Shopify store, Tier B cart-link eligible, fact allows, permitted and proven on a rail | that rail (A2A or Reap, by the order above) | handoff |
| Shopify store offering Shop Pay, shopper signed in, permission granted, wallet fact fresh, **and question 3 answered yes** | Shop MCP completion, ranked by attribution like any route (it carries none today) | handoff |
| anything else | handoff | — |

### D7. Merchant identity

- **Host-keyed spaces** (the facts, the rails' eligibility rows, the directory, allowlists) use `(normalize_domain(host), ISO-2 market)`. The fact table adds the vantage, and the router reads the buyer vantage (`MERCHANT_PURCHASABILITY_BUYER_VANTAGE`).
- **The host is the one the executor transacts on**, i.e. the cart host.
- **Aliases:** an alias table maps each storefront domain to its `*.myshopify.com` store. It is many-to-one: one store can serve several storefront domains, for example one per market. The table is built from our own crawl and merchant data. Shopify catalog results are not stored in it unless Shopify confirms that storing such ID mappings is allowed.
- **The router never matches by suffix.**
- **Portal merchant ids** (`merch_*`) belong to connected sellers, which are out of scope. They are mapped to hosts through `merchant_stores` only to apply the scope test.
- **Canary lists for this router must name what they admit: an empty list admits nothing.** That is the opposite of today's `SUBMIT_PAYMENT_MERCHANTS`, where empty means no narrowing. A list entry that cannot be mapped admits nothing.

### D8. Switches

- **The switches for this scope.** Every one of them stops payment when off or empty, and all must admit the purchase:
  1. `PAYMENT_ORCHESTRATOR_ENABLED`;
  2. each path's dial;
  3. the pilot scopes. **An unset or empty scope admits nobody.** That is the opposite of today's Reap pilot scope, where unset means no pilot and nothing refused, so it is migrated in Phase 1;
  4. the per-merchant disabled row (it holds on every path);
  5. the canary lists (D7).
- **Above both populations,** Phase 1 adds one stop that halts every agent payment, connected or not.
- **The purchasability fact is not a switch here** (D6).
- **`SUBMIT_PAYMENT` keeps its current, narrower meaning** for connected sellers: it gates their protocol-tier charges (acp, ucp, ap2) and leaves the live hosted and REST flows alone. It does not fit this scope. It keys on a portal `merchant_id`, which an unconnected seller lacks, and its empty canary list admits everyone. So `merchant_protocol` and `pivota_browser` paths are gated by switches 1 to 5, not by `SUBMIT_PAYMENT`.
- **The gateway** asks the backend router through an endpoint added in Phase 1, and treats a timeout or error as **handoff**, never as allowed. That reverses the fail-open pattern of today's gateway-to-backend reads. Gateway flags can only narrow the backend's answer.

### D9. The purchase ledger

- **Ledger changes:**
  - the executor CHECK gains `merchant_protocol` and `pivota_browser`;
  - the rail CHECK gains a value per new path;
  - the parent row stays stateless and keeps copying from the path's own child row, so each new path brings its own child table.
- **The migration and the startup heal change together.** `ensure_agent_purchase_schema` rewrites the rail CHECK to its own definition at every boot, so a migration alone would be narrowed back. Prod applies the SQL by hand, as for every migration.
- **Every router-made purchase records its basis and its dropped candidates with reasons,** including purchases routed to Reap (D1). That fixes the mislabel without changing what `agent_selected_rail` means on existing rows.
- **Idempotency:** one key per purchase. A maybe-write is read back, never resent.
- **A handoff is not a purchase.** Today only `/r` records one, on its click event (`outbound_click_events`, `handoff`/`warm_reason`). Phase 1 adds the same record for the other handoffs: a UCP escalation `continue_url` and a prefilled cart served by get_offers.
- **Refunds and disputes** follow ADR-028 D9 per path, from its `refunds` declaration.
- **Attribution:** each path's click id is minted once per purchase attempt, as ADR-028 D3 requires.

## Phases

1. **Route only, dark.**
   - Router endpoint, declarations for the two existing rails, the D6 filters read before routing, and the D7 key work. The alias table, the `merch_*` map and migrating the env lists and the Reap pilot scope to "empty admits nothing" all land here.
   - The basis/dropped fix for Reap (D9), and the direct Reap route running the router's filters (D1).
   - The one stop above both populations (D8).
   - Handoff records for UCP escalations and get_offers carts (D9).
   - Doors ask in shadow and log the answer beside what they did. With both rails dark, the shadow answer is "handoff" almost everywhere. The value is in measuring where doors and router disagree.
2. **Shop MCP completion,** only if question 3 is answered yes, behind its dial, after Shopify's permission, the Shop account connect, and a wallet fact. One merchant, one market, staff orders first (ADR-028 Phase A).
3. **The token rail's order step,** when it is built and its declaration (D2) is complete.
4. **A `card_number` path** only after question 1.

## Open dependencies

- Shopify: the Token-tier permission to complete checkouts for signed-in shoppers, its per-order approval, and attribution on Shop MCP orders.
- A probe or fact for which wallets a checkout offers, per merchant and market.
- The token-rail partner's order contract and funds flow.
- For each rail's token product: whether the rail fills the checkout (admitted) or hands the token to Pivota (D3 refuses it).
- Mastercard Agent Pay: not reviewed for this ADR.

## Questions for the owner

1. **Card numbers.** Keep D3, which excludes Shop spend requests unless a PCI-scoped executor receives the number? Or open a separate decision on a card vault, which would change ADR-028 D6 and Pivota's card-data scope?
2. **Attribution against convenience.** Shop MCP completion carries no attribution today, so D6 ranks it below an attributed rail when totals are unknown. Is that right when the buyer already holds Shop Pay and the rail needs a new enrolment?
3. **Shop MCP as a completion route.** ADR-028 D2 defines the protocol route as the merchant's protocol at the tier the merchant requires. Shop MCP's tier is granted by Shopify, not by the merchant. Admitting it means amending ADR-028 D2 to add a platform protocol route, admitted on the platform's grant plus the merchant not having opted out. Amend it, or leave Shop MCP as a handoff only?
4. **Partner attestation.** On `rail_managed` paths the buyer's authorization is the partner's attestation, not a primitive Pivota verified (D5). Is that acceptable for admission, or does a rail need to return verifiable evidence?
5. **Shadow routing.** Start Phase 1 now, before any new path exists?

## Consequences

- One answer, with recorded reasons, to "who places this order and with what credential" for every unconnected-merchant door.
- New payment programs arrive as a declaration plus an executor client.
- The purchasability sweep's coverage becomes a ceiling on which paths can be offered.
- One spine with two settlement selectors: the processor router for connected sellers, this router for unconnected ones. The boundary is the seller's connection status, defined in Scope.
- Card-data scope stays where ADR-028 put it; some programs wait on question 1.

## Alternatives considered

- **Each door keeps choosing.** Rejected: that is the present state.
- **Extend the PSP router to unconnected merchants.** Rejected: it fails over between processors within one merchant, which is the opposite of this scope's rule (no failover after the first write; a decline stops).
- **Quote every path and pick the cheapest.** Rejected: abandoned checkouts on live stores, and against Shopify's rule for Shop checkouts.
- **Pivota as processor, or holding funds.** Rejected by ADR-016.
- **A Pivota card vault.** Not taken now; it is question 1.
