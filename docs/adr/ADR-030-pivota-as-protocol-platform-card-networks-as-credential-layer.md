# ADR-030: Pivota is the UCP/ACP platform; the checkout is the merchant's; card networks are only the credential layer

**Status:** Proposed (draft 6), 2026-11-10. Nothing here is built or switched on by this ADR. Acceptance waits on the owner's questions at the end. Draft 2 aligned D3, D7 and D10 with the payment-partner flow the owner had already agreed. Drafts 3 and 4 answer the docs review of this ADR together with ADR-028 amendment 1. Draft 5 records the owner's completion rule as "not for now, conditionally later". Draft 6 applies D11 beyond the pilot, and records the answers to owner questions 2, 3 and 5.

**Decision owner:** peng. **Runtime owners:** the gateway owner (merchant protocol calls), the backend owner (records and facts).

**Direction already decided by the owner (2026-11-10):**
- Go protocol-native.
- Freeze integration-only payment work. The freeze does not cover the work the agreed payment-partner flow still needs (see D10).
- Merchant eligibility: the merchant accepts agent orders, shown by publishing an agentic checkout protocol (the UCP checkout capability, or ACP). That is the run condition for a payment partner's checkout agent, in the pilot and beyond (D11).
- For now, completion is the buyer's or a payment partner's. A payment partner's own checkout agent places the order on the merchant's checkout. Pivota does not call `complete_checkout` for now. It may later, only on the conditions in D3 and by a separate owner decision.

**Decisions this ADR follows and does not reopen:**
- **2026-11-08 (ADR-028 amendment 1, #2758):**
  - the payment partner fills the merchant's checkout, and Pivota handles no card credential. The owner's 2026-11-10 rule keeps that for now and names one later exception (D3);
  - the merchant is merchant of record on every lane;
  - a refund is a request to the merchant;
  - Pivota's own credential retrieval from a card network is out of scope until a new decision amends ADR-028 D6.
- **2026-11-08:** the payment partners are interchangeable in one orchestration layer, which is the one system of record for partner-completed purchases. Pivota-issued cards are removed.
- **2026-11-10 (the agreed partner flow):**
  - quote and payment in one checkout session;
  - the buyer approves every purchase;
  - the pilot is one card network, one currency, guest checkout;
  - the order enters the merchant through our entry link;
  - the first supervised live purchase runs on the partner that already has quote, approve and place;
  - the model where the agent polls for the credential and pays is never used.

**Builds on and amends** (all Proposed unless marked; SIGNOFF.md is unsigned):
- **ADR-014:** protocols are edge adapters around one spine. This ADR adds the *platform side*: Pivota calling a merchant's protocol, not only serving one.
- **ADR-016:** Pivota is non-custodial. Holds as written.
- **ADR-021 (Accepted):** the gateway is the protocol gateway. Merchant protocol calls are made by the gateway (D2).
- **ADR-025:** the attribution loop. D7 says where our click id rides on this lane.
- **ADR-028, read with its amendment 1 (#2758).** Amendment 1 sets the credential rule. This ADR adds the owner's 2026-11-10 rule on completion.
  - **Amended (D2, the protocol route; route-choice rule 2; Phase A; the protocol-route alternative):** Pivota does not call `complete_checkout` for now. The protocol route is not used until the owner turns it on by a separate decision, and then only on the conditions in D3.
  - **Amended (D5, the run condition, for a payment partner's checkout agent):** see D11.
  - **Amended (D7, on the payment-partner route only):** see D3, "Approval" and "Payment".
  - Pivota's own browser runner stays out, as amendment 1 says.
  - ADR-028 carries pointer notes to this ADR under its D2, D5 and D7. They cite "ADR-030 D11" in full, because ADR-028 has a D11 of its own.
- **ADR-029:** one purchase router for unconnected sellers. Its door, router, parent ledger and events are where a partner-completed purchase starts and is recorded (D3, D10). **Unchanged, except:**
  - the paths Pivota itself would execute are not admitted for now:
    - completion through a merchant's protocol, and so the `merchant_protocol` executor, until the separate owner decision in D3, and then only on D3's conditions;
    - completion through a platform protocol (its context table and its Phase 2), which does not meet D3's conditions as written;
    - Pivota's own browser route and the `pivota_browser` executor, which stay out (amendment 1);
  - a `card_number` path stays not admitted;
  - its D3 is read with ADR-028's amended D6. "Stripped on arrival" is a detector. The rule is that the partner never sends a credential;
  - its owner questions 1 (a card vault) and 3 (a platform protocol as a completion route) are answered "no" by amendment 1 and the 2026-11-10 rule, once the owner confirms (question 6 below).

  ADR-029 carries notes at each of these places that point here.

## Scope

**Terms.** A *partner agent* is an AI application that calls Pivota for its users. A *payment partner* is a card issuer or agent enabler whose checkout agent places an order with the buyer's card. The two are different parties.

**In scope:** every agent purchase on a merchant that is *not* connected to Pivota (ADR-029's definition), from the moment the buyer picks an offer until the merchant's order is closed.

**Out of scope, frozen:** connected merchants paying through their own payment provider (our `orders` table, the processor router, hosted payment links). Hosted links stay off. Nothing new is built there (D10). One rule still reaches that lane: a refund is a request to the merchant on every lane (D5).

## Context

### What exists today (origin/main, 2026-11-10)

Checkout and payment run on four lanes:

| Lane | Who creates the checkout | Who charges | Money state in our DB | State |
|---|---|---|---|---|
| A. Integrated provider checkout: our `orders` table, hosted payment page, in-process ACP lane | Pivota | the merchant's payment provider, driven by Pivota | **yes**: paid, refunded and GMV state | live for connected merchants; hosted links off; in-process ACP dark |
| B. Purchase router + rails (Reap agentic purchase, A2A token rail + VIC order step) | the rail's checkout agent, from our catalogue price | the rail, with the buyer's card | rail state + parent ledger | dark; the A2A token step is refused in production; the VIC order step has no production transport |
| C. Merchant protocol door (UCP checkout created at the merchant) | the merchant | the merchant | none | client built in both repos; `complete_checkout` refused in both; no partner-facing caller |
| D. Hand-off (`/r`, a cart or checkout `continue_url`, `esc_` ids) | the merchant, or the buyer on the storefront | the merchant | none (click rows) | live |

Most open money defects are in lane A: cancels that do not reach the payment provider, a hosted page that leaves a second order, stuck-paid checks on our own rows, settlement jobs from an older business model. They are fixes to plumbing that a merchant's own checkout makes unnecessary. Lane B is the built, dark home of the agreed partner flow; its open items are the partners' missing pieces (an order API on one, a first real purchase on the other).

Lane C already does more than its "one caller" suggests:
- **Gateway: `src/services/ucpBuyerAgentClient.js`.**
  - Discovers a merchant's `/.well-known/ucp` and picks its MCP service.
  - Sends our platform profile (`meta["ucp-agent"].profile`, and the `UCP-Agent` header when signing).
  - Signs requests with RFC 9421 (ECDSA P-256), and supports the anonymous, signed and token trust tiers.
  - Calls `create_cart`, `create_checkout`, `update_checkout` and `get_checkout`.
  - `complete_checkout` is refused before any I/O.
- **Gateway: `mcp-server/src/ucpCheckoutEscalation.js`.**
  - Answers our own UCP checkout with `requires_escalation` and a `continue_url`.
  - With `AGENT_CHECKOUT_UCP_MERCHANT_PRICING_ENABLED` (default off), the `continue_url` and totals come from the merchant's own `create_cart`. Without it, they come from our link.
- **Backend: `services/merchant_ucp_checkout.py`.**
  - Creates, updates and reads a checkout on the merchant's door.
  - `build_attribution` puts our click id in the UCP `attribution` member, using the merchant's own field names.
  - `complete_checkout` is excluded by an allowlist.
  - Its only caller reads a merchant quote to cap a card (`services/agent_card_issuance.py`, refused in production).
- **Merchant facts on file.** `execution_routes` (migration 196) stores a merchant's UCP endpoint and has a profile-fingerprint column, and `evidence_items` stores the tool names it answered. Nothing stores the merchant's spec version, capability list or payment handlers.
- **What we declare.**
  - Our platform profile (`/.well-known/ucp-agent`) does not request `dev.ucp.shopping.order`, declares no `webhook_url`, and has `payment_handlers: {}`.
  - An inbound UCP order-webhook receiver exists but is dark, keeps only in-memory metadata, and has no replay guard.
- **Order reads.** `get_order` answers rail ids (`pp_`, `rp_`, `ap_`) and kernel orders only. An `esc_` id answers "not found".

### What the protocols provide (UCP 2026-08-25, ACP 2026-04-17)

- **UCP checkout.**
  - Operations are `create / get / update / complete / cancel_checkout`.
  - Statuses: `incomplete`, `requires_escalation`, `ready_for_complete`, `complete_in_progress`, `completed`, `canceled`.
  - The business MUST return a `continue_url` on `requires_escalation`. The platform SHOULD prefer it to a link it builds itself.
  - The business's `totals[]` are authoritative; the platform does not recompute them.
  - Idempotency keys are required on MCP `complete_checkout` and `cancel_checkout`.
- **UCP order.**
  - `get_order` returns a full snapshot: line items, an append-only fulfilment event log, and adjustments (refund, return, cancellation, dispute).
  - The business signs webhooks and POSTs the full order to the platform's `webhook_url`. That URL is declared in the platform profile under the order capability's config, or agreed at partner onboarding.
  - Some merchant platforms gate order reads and webhooks behind their own partner onboarding and a token tier.
- **UCP payment.**
  - The business lists payment handlers. The platform submits exactly one instrument in `complete`.
  - Base credential schemas include `network_token` (number, cryptogram, ECI, token requestor id), which the spec names as the way a platform stays out of card-data scope.
  - No handler is named for a specific card network's agent program. A network agent token rides only where the merchant's handler accepts `network_token`.
  - AP2 mandates are an optional extension.
- **UCP attribution.** `attribution` is a free-form map accepted on cart and checkout calls. The business MAY copy it onto the order. It is informational: it does not by itself credit an affiliate network.
- **Completion on a large merchant platform.**
  - `complete_checkout` needs that platform's token tier *and* a per-application grant that is not publicly obtainable.
  - Otherwise the documented path is "send the buyer to `continue_url`".
- **ACP.**
  - The merchant hosts `checkout_sessions` (create, update, get, complete, cancel).
  - Payment is a delegated-payment vault token from the merchant's payment provider, which can wrap a network token and cryptogram.
  - Order events are webhooks signed with HMAC.
  - `affiliate_attribution` (provider, publisher id, sub-id) exists, but its RFC is still "Proposal".
  - `/.well-known/acp.json` discovery exists, but credentials are bilateral: each merchant issues them to each agent platform.
- **Card networks.**
  - One network's merchant-side "connect" programme accepts agent payments over its trusted-agent protocol, ACP and UCP (pilot).
  - Its agent-credential API is restricted.
  - The other network's agent token travels in standard card fields and is authorised by the merchant's payment provider like any network token.

## Decision

### D1. Pivota's role: the platform, never the merchant or the payer

- For every partner agent, Pivota acts as the **UCP platform** (and, where a merchant offers it, the **ACP agent**).
- On path 1 (D3), Pivota creates the checkout at the merchant, through the merchant's own door. On path 2, the payment partner's agent opens the merchant's own checkout. **On both, the merchant's checkout is the order of record.**
- The merchant's price, tax, shipping and discounts are authoritative and are shown verbatim. Pivota does not quote a total of its own on this lane.
- ADR-016 holds. Pivota never receives, holds or settles funds, is never the merchant of record, and writes **no payment state of its own** for these orders: no paid flag, no refund row, no settlement row. It records references, hand-offs and what the merchant reports. On path 2, the status in the purchase record is copied from the payment partner's report.

### D2. One caller of merchant doors: the gateway

- Only the gateway calls a merchant's protocol door. It holds the platform profile and its signing key (ADR-021). The backend asks the gateway through the existing internal-route pattern; the warm hand-off already does this.
- The backend owns the **records**:
  - merchant capability facts (D8);
  - the hand-off record of each merchant checkout (the storefront hand-off record, in review as a separate change);
  - order snapshots (D4).
- `services/merchant_ucp_checkout.py` gets no new callers; a second transport would be a second thing to keep correct. Its one reader today is the card-issuance service, which D10 removes. When that goes, the module has no caller and goes with it, after its attribution field rules are ported to the gateway.
- The buyer client stops pinning one spec version. It negotiates against the merchant's `version` / `supported_versions` and records the version that was used.

### D3. Who completes the payment

**Pivota does not call `complete_checkout` for now** (owner, 2026-11-10)**, and for now it handles no card credential** (ADR-028 amendment 1). For now, a checkout completes in one of two ways. "Later, conditionally", at the end of this section, says when Pivota itself may complete.

**Market, on both paths.** A merchant checkout is created, and a partner quote is requested, only for a **named** buyer market, in that market's currency (ADR-026, ADR-029 D6). A request that names no market, or whose market is assumed, gets no merchant checkout and no quote. It is answered as it is today, labelled as assumed.

**Path 1. The buyer completes at the merchant.** Pivota creates the checkout at the merchant (`create_checkout`, not `create_cart`), with the buyer's selection, the named market and our attribution (D7). The partner agent gets back:
- the merchant's `continue_url`;
- the merchant's totals and messages;
- an `esc_` id.

The buyer pays on the merchant's own checkout.

**Path 2. A payment partner's checkout agent places the order on the merchant's own checkout.** This is the flow already agreed with the payment partners. This ADR adopts it as it stands.
- **Start.** The purchase starts at the existing rail-neutral purchase door (ADR-029). Pivota checks the price and the cap, picks the payment partner, and records the purchase.
- **Card.** First time only, the buyer binds a card on the partner's hosted page.
- **Quote.** Pivota sends the partner: our reference, the entry link, the cart link, the items, the currency of the buyer's market and the ship-to. The partner's agent opens the merchant's checkout through the entry link, then the cart link byte for byte, keeping cookies. It returns the merchant's items, discount, shipping, tax and total. Nothing is charged.
- **Approval.** The buyer approves **every** purchase, on the partner's page that shows the merchant and the quoted total.
- **Payment.** In the same checkout session as the quote, payment is for exactly the approved total.
  - If the session cannot be held, the partner re-opens it. A lower total is then paid without re-asking only when the seller of record, the items and the currency are unchanged. Any other change re-asks the buyer.
  - Above the approved total, the purchase fails with no charge.
  - This amends ADR-028 D7 for the payment-partner route only. D7 stays exact everywhere else, and ADR-029 D5's check before the buyer's authorization is unchanged.
  - The credential exists only on the partner's side. The merchant's own processor charges the card; the partner's charge and capture interfaces are not used.
- **Completion.** It carries the merchant's order number, the total and the currency. An order without them is held for a person, never marked complete.
- **After.** The partner cancels one order on request. A refund is a request to the merchant. The partner reports refunds and chargebacks, and gives a daily order list for reconciliation.

**What stays on the partner's side.** How the partner's agent reaches the merchant's checkout is the partner's concern: form fill today, the merchant's protocol door or a credential delivered to the merchant's processor later. The contract with Pivota does not change with it. Pivota creates no merchant checkout of its own for a path-2 purchase.

**Pilot scope for path 2:**
- one card network, one currency, guest checkout;
- merchants in one market that meet the run condition (D11);
- the partner's agent identifies itself, and stops on any CAPTCHA, bot challenge or issuer step-up (D11);
- the first supervised live purchase runs on the partner that already has quote, approve and place, and is reconciled three ways: Pivota, the partner, the merchant's order.

**Not used, on any path:** the model where an agent polls a partner or a card network for the credential and pays.

**Later, conditionally.** Pivota may later call `complete_checkout` itself. The owner's rule (2026-11-10) allows it only when all of these hold:
- the checkout is one the merchant publishes through UCP or ACP;
- the payment is a single-use network token issued under a card network's agent programme (Visa Intelligent Commerce, Mastercard Agent Pay), scoped to that merchant and that amount;
- the amount is the amount the buyer approved;
- the merchant is the merchant of record;
- Pivota never holds funds.

**The one credential Pivota may then carry.** On this path, and only on it, Pivota may pass that single-use, merchant- and amount-scoped network token, with its cryptogram, to the merchant's payment handler.
- Pivota never stores or logs them.
- The card number and the security code never pass through Pivota. "Pivota never touches card data" means those two.
- Pivota carries no other credential, and none on any other path.

**Why it is acceptable in principle** (owner, 2026-11-10). Merchants want orders from every channel. A merchant that accepts the order and the payment through UCP, or through a card network's agent programme, has accepted the transaction.

**Turning it on needs a PCI and compliance assessment and a separate owner decision.** Until then:
- Pivota handles no card credential at all.
- Nothing here authorises a call to `complete_checkout`, a card-network credential call, or any code that holds, forwards or enters a credential.
- `complete_checkout` stays structurally refused in both clients, and ADR-028 amendment 1 stays in force.

**What that decision has to settle:**
- the PCI and compliance assessment: the card-data scope of carrying the token and its cryptogram in transit;
- where the token comes from: minted by a payment partner, or retrieved by Pivota from the card network. Amendment 1 ties Pivota's own retrieval to a new decision amending D6;
- per merchant, a payment handler that accepts the token, and a completion grant at a trust tier Pivota holds.

### D4. Order status comes from the merchant, never from inference

Sources, in order:
1. **Merchant webhooks** to Pivota's `webhook_url`.
   - Pivota requests `dev.ucp.shopping.order` in its platform profile and declares the URL, behind a flag.
   - Every event is verified: the signature, that the signer's profile is the order's business, a replay window, and an id seen once.
   - The full order snapshot is kept, minus buyer contact fields and any payment-instrument fields.
2. **The merchant's `get_order`**, where the merchant grants it to Pivota.
3. **The payment partner's report,** on path 2: status read by our reference, with the merchant's order number, total and currency on completion, plus the partner's refund and chargeback notices and its daily order list.
4. **The affiliate network's report** (commission import), as a late, partial confirmation.

Partner-facing `get_order` on an `esc_` id answers only what a source says, labelled with the source and its time. With no source it answers `handed_off`, never "order observed" inferred from a click (a review finding on the hand-off record).

### D5. Cancel and after-sales

- **Before completion, path 1:** `cancel_checkout` at the merchant, with an idempotency key. A checkout the partner agent abandons expires at the merchant, so there is nothing of ours to clean up.
- **Before completion, path 2:** the payment partner cancels that one order, idempotently and with a definite answer. Never a card-wide removal.
- **After completion:** the merchant handles refunds, returns and disputes (the existing "refund is the merchant's" rule stands). Pivota surfaces the order's adjustments and fulfilment events. It records after-sales requests and points the buyer to the merchant. It never executes a refund.
- **The refund rule holds on every lane** (ADR-028 D11, added by its amendment 1), lane A included. Known contradiction, for a follow-up: the agent refund routes already refuse, but staff and operations refund routes on lane A still execute a refund on the merchant's payment provider (owner question 7).

### D6. Identity: one platform profile; partner agents are internal

- One Pivota platform profile and signing key serves all partner agents. Per-partner profiles are not adopted now: trust tiers and grants on merchant platforms attach to the profile or token holder, and per-partner profiles would split every grant.
- Partner identity is Pivota-internal: the forwarded partner-identity assertion (in review as a separate change). Every merchant checkout, hand-off record, click and order snapshot is keyed to the partner agent that asked for it. `get_order` and cancel are refused across partners.
- **Merchants see our click id, not the partner agent.** The attribution map carries no partner name.
- Whose identity the merchant sees when a payment partner's agent checks out is an open question to the partners (D11).
- Merchant rate limits are shared across partners, so partner rate isolation is enforced at Pivota's door, before a merchant call.

### D7. Attribution: our click id rides inside the checkout; commission rides the network

- **A partner-completed order (D3 path 2) enters the merchant through our entry link.** The entry link is our network tracking link, with our click id as the network sub-id. The partner's agent loads it first, follows its redirects and keeps its cookies, then opens the cart link byte for byte. The cart link carries our click id as a cart attribute. There is exactly one entry-link load per buyer order attempt, never one from a quote retry or a probe (ADR-028 D3). The entry link is a required field of the partner contract.
- **Every merchant checkout Pivota creates (D3 path 1)** carries `attribution` with our click id, as `click_id_tag` / `click_id_value` (the pair already used on referral URLs), plus `referring_domain`. Because update is a full replacement, it is re-sent on every update.
- **Where an ACP merchant offers `affiliate_attribution`:** the network provider, our publisher id, and our click id as the sub-id.
- **Labels are not commission.** A checkout created server-side skips the network's tracking link. Per merchant and market, a stored fact records which path carries commission:
  - **network-tracked:** the buyer, or the partner's checkout agent, enters through our tracking link, with our click id as the network sub-id. Each programme's terms must allow an order placed by a checkout agent, and the first orders are checked by hand in the network's report;
  - **protocol-attributed:** the network or merchant credits by the attribution member;
  - **none.**

  Unattributed completed orders follow ADR-028 D4: the order first, then a task to fix the loop. They never change an offer's rank (ADR-025).
- A `continue_url` that carries an access key MUST NOT be passed through a third-party redirect.

### D8. Merchant capability facts are stored, per domain

- **A background job** reads each candidate merchant's `/.well-known/ucp` and `/.well-known/acp.json`. It runs from the crawl egress, paced with the other crawl jobs, under a per-host daily cap, and never creates a checkout. It stores, per domain:
  - spec version and supported versions;
  - services and transports, with endpoints;
  - capabilities and extensions;
  - payment-handler ids, with the credential types they accept;
  - ACP discovery fields;
  - fetch time, and the profile fingerprint (`execution_routes` already has the column; the sweep fills it).

  `execution_routes` is the route identity; the profile facts sit beside it.
- **Eligibility is a stored fact:** the merchant publishes the UCP checkout capability, or ACP (D11). The protocol directory and the doors read it. Nothing is decided by a request-time fetch.
- **The purchasability gate keeps its role:** can this merchant sell to this buyer's market.

### D9. Switches

Every new behaviour defaults off. With its flag off, every answer is byte-identical to today's. Proposed flags:

| Flag | Repo | Effect |
|---|---|---|
| `AGENT_CHECKOUT_UCP_MERCHANT_CHECKOUT_ENABLED` | gateway | the escalation door creates a merchant **checkout** (not a cart) and returns its `continue_url`, totals and messages |
| `MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED` | backend job | the D8 capability sweep |
| `UCP_BUYER_AGENT_ORDER_CAPABILITY_ENABLED` | gateway | our platform profile requests the order capability and declares `webhook_url` |
| `UCP_ORDER_WEBHOOK_PERSIST_ENABLED` | gateway + backend | verified merchant order events are stored and served to `get_order` |

- Path 2 adds no new switch. It runs behind the existing purchase-door and per-partner dials (ADR-029 D8), all off in production today.
- No switch exists for Pivota's own completion; `complete_checkout` stays structurally refused in both clients until the separate owner decision (D3).
- The existing `MERCHANT_PURCHASABILITY_GATE_ENABLED` and the agent-checkout kill switch apply to every one of them.

### D10. Reuse, freeze, retire

| Component | Decision |
|---|---|
| Gateway UCP buyer client (discovery, negotiation, signing, trust tiers) | **Reuse**: the one transport (D2) |
| Escalation door and `esc_` ids; the storefront hand-off record | **Reuse, with one change.** Today's seller-priced `esc_` id embeds the seller's cart id. Under this ADR an `esc_` id names a merchant checkout without carrying its id: the merchant checkout id and `continue_url` live server-side in the hand-off record, and are never logged |
| Backend `build_attribution` | **Port** its field rules to the gateway. The backend module goes with card issuance (D2) |
| `execution_routes`, store-audit probe | **Reuse**: route identity; the D8 sweep extends it |
| Merchant purchasability gate; partner identity; partner rate isolation; affiliate sub-id | **Keep** |
| UCP order-webhook receiver | **Reuse and harden** (D4) |
| Card-network client, trusted-agent verifier | **Keep** at connectivity and recognition only. No credential call for now (ADR-028 amendment 1; D3) |
| Lane A: `orders`, processor router, hosted page, payment finalizer | **Frozen** for connected merchants; hosted links stay off; no new builds. The refund rule still applies to it (D5); its staff and operations refund routes are a follow-up |
| In-process ACP lane, AP2 routes | **Frozen dark** |
| Purchase door, router, parent ledger, purchase events (ADR-029) | **Keep**: the one system of record for partner-completed purchases (D3 path 2). Not retired |
| The two partner rails (Reap agentic purchase; the A2A token rail and its order step) | **Keep as built, dark.** Work that the agreed partner flow needs waits on the partner's side: a first supervised live purchase on one, an order specification and sandbox on the other. The freeze stops only custom plumbing outside the agreed contract; the agreed flow's own remaining work continues (owner, 2026-11-10) |
| The A2A rail's token step (returns a credential to the caller) | **Refused in production in code** (#2759), whatever any switch says |
| Pivota-issued cards (`/agent/v1/cards`), refused in production | **Remove** (owner decision, 2026-11-08) |
| Settlement and transfer jobs | **Paused**; retire with the affiliate payout plan |

### D11. The run condition for a payment partner's checkout agent (amends ADR-028 D5)

- **Scope.** Only a payment partner's checkout agent, on any merchant that meets the condition, in the pilot and beyond (owner, 2026-11-10; the first version of this section was pilot-only). Pivota's own runner stays out.
- **Basis** (owner, 2026-11-10). Merchants want orders from every channel. A merchant that accepts the order and the payment through an agentic checkout protocol, or through a card network's agent programme, has accepted the transaction. A merchant that challenges, blocks or opts out has not, and the rules below stop there.
- **The condition.** The merchant publishes an agentic checkout protocol: a UCP profile that lists the checkout capability, or ACP checkout. It is read from the stored fact (D8), never from a request-time fetch.
- **Also sufficient** (owner, 2026-11-10). The merchant, or its edge, verifies the agent under a card network's agent programme. This is ADR-028 D5's "verified recognition", observed and recorded.
- **What it changes in D5, for that agent:**
  - D5's entry list gains the condition above;
  - D5's interim clause no longer applies. That clause allowed the browser route only under the merchant's written permission, or a protocol grant whose terms cover browser completion, until the robots-and-terms decision was made.
- **Robots and terms** (owner, 2026-11-10). ADR-028 D5 requires the owner's decision on robots and terms before the browser route takes its first order. For a payment partner's checkout agent, this section **is** that decision: a buyer-initiated order, placed by an agent that identifies itself, on a merchant that meets the condition, may proceed. No further check of robots rules or store terms is required.
- **What it does not decide.** Robots and terms for any agent other than a payment partner's.
- **D5 rules that stand unchanged:**
  - a merchant that opts out is never completed;
  - no CAPTCHA solving, address rotation, or disguised user agent or fingerprint;
  - stop on any CAPTCHA, bot challenge, block or issuer step-up, never routed to a solver, a person or the buyer;
  - guest checkout only;
  - the probe rules;
  - the cap on attempts per order.
- **After a challenge or block,** that merchant is not completable by the partner's agent. As D5 says, it is re-enabled only on evidence: verified recognition, or the merchant's written permission. Publishing the protocol does not re-enable it.
- **Identity.** D5 requires every completion request to be signed with Pivota's agent identity. On this route the request is the partner's. The partner's agent MUST identify itself as an agent. Whether it does so under the partner's identity or under one registered for Pivota is an open question to the partners (open spec question 5). Until it is answered, D5's signing rule reads "the agent identifies itself" for this route.

## Each flow: today vs protocol-native

| Flow | Today | Protocol-native (this ADR) |
|---|---|---|
| Discovery | Our catalogue; merchant `/.well-known/ucp` fetched at request time, in-memory cache; version pinned | Stored per-domain profile facts (D8); negotiated version |
| Merchant decision | Purchasability gate; directory from cart-link and purchasability tables | + "publishes UCP checkout / ACP" fact, which is the eligibility rule; the directory reads it |
| Checkout URL | `/r` link or a merchant **cart** `continue_url` | Path 1: merchant **checkout** `continue_url`, merchant totals, our attribution inside. Path 2: our entry link, then the cart link, opened by the payment partner's agent |
| Payment completion | Hosted page (off), partner rails (dark), or buyer on storefront | Path 1: the buyer at `continue_url`. Path 2: the payment partner's checkout agent, after the buyer approves the quoted total. Not Pivota for now (D3) |
| Order status | None after hand-off; `esc_` not found | Merchant webhooks / `get_order` where granted; the payment partner's report; the affiliate report |
| Cancel / after-sales | Cancel flips our row; agent refund routes refuse, staff and operations refund routes still execute | `cancel_checkout` at the merchant (path 1) or the partner's per-order cancel (path 2); adjustments surfaced; the merchant refunds |

## What Pivota signs and holds

- **Signs:**
  - its platform profile (published keys);
  - every request to a merchant door (RFC 9421, under that profile).
- **Holds:**
  - merchant capability facts;
  - the hand-off record (partner agent, click id, merchant, merchant checkout id, `continue_url`, status, source, times);
  - the purchase record of a partner-completed order (our reference, the quote, the approved total, the status as the partner reports it, the merchant's order number, total and currency);
  - verified order snapshots without buyer contact or payment-instrument fields.

  The merchant checkout id and `continue_url` are bearer-like and are kept out of logs.
- **Never holds:**
  - funds;
  - a card number or a security code, in any process, log or store.
- **For now handles no card credential at all.** Later, on D3's conditional path only, it may pass a single-use network token and its cryptogram to the merchant's payment handler, in transit, never stored or logged.

## Phases

1. **Census.** Read-only: what pilot merchants publish today (D8 facts, first from stored evidence, then the sweep). It also narrows the payment partners' pilot merchant list to those that meet the run condition (D11).
2. **Merchant checkout + hand-off (path 1).** Merchant checkout `continue_url` with merchant totals; `esc_` → hand-off record; `cancel_checkout`.
3. **Order leg.** Order capability requested, webhook persisted, `get_order` on `esc_` from sources.
4. **Partner-completed purchase (path 2), as agreed.** The supervised live purchase on the partner that already has quote, approve and place. The other partner's order step once its specification and sandbox exist. This phase is paced by the partners, not by phases 1 to 3.
5. **ACP adapter.** Only if the census finds ACP merchants and they issue us credentials.

Pivota's own completion is not a phase of this ADR (D3, "Later, conditionally").

## Risks

- **Commission leakage.** A protocol checkout bypasses network tracking. Revenue depends on the per-merchant attribution fact (D7), on the entry link being honoured by the payment partner's agent, and on networks crediting an order a checkout agent placed. None is proven yet.
- **The payment partner's checkout agent is the weak link.** Its success rate, its behaviour on bot challenges, and whether it can hold a session through the buyer's approval are the partner's, and are unmeasured on real merchants.
- **Order visibility.** Merchant webhooks may need per-platform partner onboarding. Until then path 1 shows `handed_off` only.
- **Version drift.** Merchants move spec versions without notice; negotiation and stored versions replace the pin.
- **Abandoned checkouts.** Creating a merchant checkout before the buyer is ready creates abandoned checkouts in merchant admin. Create on buyer intent only (never on search, preview or probe).

## Open spec questions

1. The sub-agent or partner identifier inside one platform profile: undefined.
2. Webhook fan-out from a platform to its downstream partners: undefined. Pivota serves it through `get_order`.
3. A handler that names a network agent token, and whether a merchant's payment provider accepts it as `network_token`: per merchant. It matters to the payment partners' longer-term route, and to any later decision on Pivota's own completion.
4. ACP: how the order webhook URL and the shared secret are provisioned (bilateral), and whether `affiliate_attribution` leaves "Proposal".
5. Whose identity a merchant sees when a payment partner's agent checks out: the partner's or Pivota's.

## Questions for the owner

1. **First milestone.** Path 1: merchant checkout `continue_url` with merchant totals, buyer pays at the merchant. OK as the first build?
2. *Answered 2026-11-10, see below.* (The order capability.)
3. *Answered 2026-11-10, see below.* (Lane A.)
4. **Own completion later.** The rule is "not for now; later only on D3's conditions". Still open: when to turn it on (it needs the PCI and compliance assessment and a separate decision), and where the token comes from.
5. *Answered 2026-11-10, see below.* (Robots and terms beyond the pilot.)
6. **ADR-029 questions 1 and 3.** Confirm both are closed as "no": no card vault, and no platform protocol as a completion route for Pivota.
7. **Lane A refunds.** Staff and operations refund routes still execute a refund. Remove them, or treat reversing Pivota's own failed orchestration as a separate case (amendment 1 leaves that open)?
8. **The approved total on the payment-partner route.** Confirm D3's rule: exact in one session; on a re-opened session a lower total is paid without re-asking only with the same seller, items and currency.
9. **ADR-028 D2's route-choice rule** is still "pending owner acceptance". With only path 1 and path 2 left, is it withdrawn?

**Owner statements this ADR records, to be confirmed at merge:**
- the freeze does not cover the agreed payment-partner flow (answered below);
- the completion rule: Pivota does not call `complete_checkout` for now; later only on D3's conditions and by a separate owner decision (D3, "Later, conditionally");
- that on that later path Pivota may pass a single-use, merchant- and amount-scoped network token and its cryptogram to the merchant's payment handler, never stored or logged, with the card number and the security code never passing through Pivota (D3);
- the payment partner's report as a source of order status on path 2 (D4);
- the run condition, in the pilot and beyond, with no separate merchant permission sought (D11);
- that the run condition is the robots-and-terms decision ADR-028 D5 requires, for a payment partner's checkout agent on any merchant that meets it (D11);
- the six points of the agreed partner flow of 2026-11-10 (listed at the top);
- the single orchestration layer of 2026-11-08, with Pivota-issued cards removed (listed at the top; D10).

**Answered (owner, 2026-11-10): D11 beyond the pilot.** D11 applies to every merchant that meets its condition, not only pilot merchants, and it is the robots-and-terms decision for a payment partner's checkout agent there too.

**Answered (owner, 2026-11-10): the order capability.** Yes. Pivota requests the order capability in its public platform profile and declares a webhook URL, behind a flag that defaults off (D4, D9).

**Answered (owner, 2026-11-10): lane A.** It stays frozen as it is for connected merchants. No retirement is scheduled now; it is revisited once the protocol lane has carried real orders. The refund rule still applies to it (D5).

**Answered (owner, 2026-11-10): the freeze and the agreed partner flow.** The freeze stops only custom rail work outside the agreed contract. The agreed flow's remaining work on our side of the partner rail continues: the single-session quote step, the entry link on the order, the transport to the partner's sandbox, a held state and a cancel path.

## Consequences

- **Fewer money defects.** New money behaviour moves to the merchant's checkout, and lane A's open defects stop growing.
- **Partner agents get the merchant's real total** in the agent before the buyer leaves or approves.
- **Order status depends on merchants and payment partners,** not on our own payment state.
- **The agreed partner flow is unchanged.** This ADR adds the merchant-checkout hand-off, the stored capability facts and the order leg around it.
- **Revenue needs a per-merchant attribution fact,** and that has to be proved order by order.

## Alternatives considered

- **Keep building lane A.** Rejected by the owner on 2026-11-10: the defects are in custom plumbing.
- **Require the payment partner's agent to use the merchant's protocol door now.** Rejected: the agreed contract is checkout fill through our entry link and cart link, and how the partner reaches the checkout is its own concern.
- **Pivota completes the checkout itself now.** Not for now (owner, 2026-11-10). Allowed later only on D3's conditions and by a separate owner decision.
- **Per-partner platform profiles.** Rejected for now (D6).
