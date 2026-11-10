# ADR-030: Pivota is the UCP/ACP platform; the checkout is the merchant's; card networks are only the credential layer

**Status:** Proposed (draft 1), 2026-11-10. Nothing here is built or switched on by this ADR. Acceptance waits on the owner's questions at the end.

**Decision owner:** the founder. **Runtime owners:** the gateway owner (merchant protocol calls), the backend owner (records and facts).

**Direction already decided by the owner (2026-11-10):**
- Go protocol-native.
- Freeze integration-only payment work.
- Merchant eligibility for the pilot is "publishes UCP or ACP"; no separate merchant permission is sought.
- Card-layer partners' own agents call the merchant's protocol door now. Pivota builds its own completion later, with a credential minted by a card-layer partner or fetched from a card network directly.

**Builds on and amends** (all Proposed unless marked; SIGNOFF.md is unsigned):
- **ADR-014:** protocols are edge adapters around one spine. This ADR adds the *platform side*: Pivota calling a merchant's protocol, not only serving one.
- **ADR-016:** Pivota is non-custodial. Holds as written.
- **ADR-021 (Accepted):** the gateway is the protocol gateway. Merchant protocol calls are made by the gateway (D2).
- **ADR-025:** the attribution loop. D7 says where our click id rides on this lane.
- **ADR-028:** Pivota completes the order on the merchant's checkout.
  - **Amended:** in phase 1 Pivota does not complete (D3).
  - **Amended:** D5's run condition no longer needs merchant permission for the *protocol* route. A merchant that publishes the protocol is eligible.
  - The browser route is out of scope here and unchanged.
- **ADR-029:** one purchase router for unconnected sellers. **Amended:** a rail no longer places orders on a Pivota-chosen storefront path. It is a card-layer partner whose agent uses the merchant's protocol door (D3), and the router's new build is frozen (D10).

## Scope

**In scope:** every agent purchase on a merchant that is *not* connected to Pivota (ADR-029's definition), from the moment the buyer picks an offer until the merchant's order is closed.

**Out of scope, frozen as-is:** connected merchants paying through their own payment provider (our `orders` table, the processor router, hosted payment links). Hosted links stay off. Nothing new is built there (D10).

## Context

### What exists today (origin/main, 2026-11-10)

Checkout and payment run on four lanes:

| Lane | Who creates the checkout | Who charges | Money state in our DB | State |
|---|---|---|---|---|
| A. Integrated provider checkout: our `orders` table, hosted payment page, in-process ACP lane | Pivota | the merchant's payment provider, driven by Pivota | **yes**: paid, refunded and GMV state | live for connected merchants; hosted links off; in-process ACP dark |
| B. Purchase router + rails (Reap agentic purchase, A2A token rail + VIC order step) | the rail's checkout agent, from our catalogue price | the rail, with the buyer's card | rail state + parent ledger | dark; the A2A token step is refused in production; the VIC order step has no production transport |
| C. Merchant protocol door (UCP checkout created at the merchant) | the merchant | the merchant | none | client built in both repos; `complete_checkout` refused in both; no partner-facing caller |
| D. Hand-off (`/r`, a cart or checkout `continue_url`, `esc_` ids) | the merchant, or the buyer on the storefront | the merchant | none (click rows) | live |

Most open money defects are in lanes A and B: cancels that do not reach the payment provider, a hosted page that leaves a second order, stuck-paid checks on our own rows, settlement jobs from an older business model. They are fixes to plumbing that a merchant's own protocol makes unnecessary.

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
- **Merchant facts on file.** `execution_routes` (migration 196) stores a merchant's UCP endpoint, and `evidence_items` stores the tool names it answered. Nothing stores the merchant's spec version, capability list or payment handlers.
- **What we declare.**
  - Our platform profile (`/.well-known/ucp-agent`) does not request `dev.ucp.shopping.order`, declares no `webhook_url`, and has `payment_handlers: {}`.
  - An inbound UCP order-webhook receiver exists but is dark, keeps only in-memory metadata, and has no replay guard.
- **Order reads.** `get_order` answers rail ids (`pp_`, `rp_`) and kernel orders only. An `esc_` id answers "not found".

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
- The checkout is created at the merchant, through the merchant's own door. **The merchant's checkout is the order of record.**
- The merchant's price, tax, shipping and discounts are authoritative and are shown verbatim. Pivota does not quote a total of its own on this lane.
- ADR-016 holds. Pivota never receives, holds or settles funds, is never the merchant of record, and writes **no payment state of its own** for these orders: no paid flag, no refund row, no settlement row. It records references, hand-offs and what the merchant reports.

### D2. One caller of merchant doors: the gateway

- Only the gateway calls a merchant's protocol door. It holds the platform profile and its signing key (ADR-021). The backend asks the gateway through the existing internal-route pattern; the warm hand-off already does this.
- The backend owns the **records**:
  - merchant capability facts (D8);
  - the hand-off record of each merchant checkout (the O-02 record);
  - order snapshots (D4).
- `services/merchant_ucp_checkout.py` keeps `get_checkout` for its one reader. Its `create_checkout` and `update_checkout` get no new callers; a second transport would be a second thing to keep correct.
- The buyer client stops pinning one spec version. It negotiates against the merchant's `version` / `supported_versions` and records the version that was used.

### D3. Who completes the payment, in two phases

**Phase 1 (now): Pivota never calls `complete_checkout`.** A checkout completes in one of two ways.

1. **The buyer completes at the merchant.** Pivota creates the checkout at the merchant (`create_checkout`, not `create_cart`), with the buyer's selection, market and our attribution (D7). The partner gets back:
   - the merchant's `continue_url`;
   - the merchant's totals and messages;
   - an `esc_` id.

   The buyer pays on the merchant's own checkout.
2. **A card-layer partner's agent completes at the merchant.** The partner is a card issuer or agent enabler, such as Reap or the A2A provider. Its agent calls the merchant's UCP or ACP door **under its own platform identity**, using its own credential. Pivota hands it:
   - the merchant's door, the item and variant;
   - the buyer's market;
   - our attribution map;
   - the buyer-approved ceiling.

   It returns:
   - the merchant's checkout id and status;
   - on completion, the merchant's order id and total.

   Pivota does not create a parallel checkout for that purchase. A UCP checkout is bound to the platform that created it, and the specs do not define handing one to another platform.

   **Pivota never receives the credential in phase 1.**

**Phase 2 (later, own capability): Pivota completes.**
- **Credential source:** minted by a card-layer partner, or fetched by Pivota from a card network's agent programme.
- **How it is submitted:**
  - as the merchant's UCP payment instrument (`network_token` credential, or the handler the merchant lists);
  - or as an ACP delegated-payment token from the merchant's payment provider.
- **Where it may run:** only on a merchant that both
  - lists a handler accepting that credential type, and
  - grants completion at a trust tier Pivota holds.
- **Rules that apply:**
  - ADR-028 D6: token, cryptogram and dynamic code in memory for one attempt only, never stored or logged.
  - ADR-028 D7: the buyer confirms exactly the merchant's total. Where the merchant offers AP2 mandates, the mandate is that confirmation.
- **Gate before phase 2:** this ADR is amended with:
  - the card-data scope of each credential path, assessed before that path takes a payment;
  - the guardrail that Pivota never receives a credential, rewritten as "never stored, never logged, one attempt, merchant-bound".

### D4. Order status comes from the merchant, never from inference

Sources, in order:
1. **Merchant webhooks** to Pivota's `webhook_url`.
   - Pivota requests `dev.ucp.shopping.order` in its platform profile and declares the URL, behind a flag.
   - Every event is verified: the signature, that the signer's profile is the order's business, a replay window, and an id seen once.
   - The full order snapshot is kept, minus buyer contact fields.
2. **The merchant's `get_order`**, where the merchant grants it to Pivota.
3. **The card-layer partner's report,** in the phase-1 rail case: the merchant order id and status the partner's agent got back. Merchant webhooks go to that partner's own platform, so it must forward them or answer status reads.
4. **The affiliate network's report** (commission import), as a late, partial confirmation.

Partner-facing `get_order` on an `esc_` id answers only what a source says, labelled with the source and its time. With no source it answers `handed_off`, never "order observed" inferred from a click (the O-02 review finding).

### D5. Cancel and after-sales

- **Before completion:** `cancel_checkout` at the merchant, with an idempotency key. A checkout the partner abandons expires at the merchant, so there is nothing of ours to clean up.
- **After completion:** the merchant handles refunds, returns and disputes (the existing "refund is the merchant's" rule stands). Pivota surfaces the order's adjustments and fulfilment events. It records after-sales requests and points the buyer to the merchant. It never executes a refund on this lane.

### D6. Identity: one platform profile; partners are internal

- One Pivota platform profile and signing key serves all partners. Per-partner profiles are not adopted now: trust tiers and grants on merchant platforms attach to the profile or token holder, and per-partner profiles would split every grant.
- Partner identity is Pivota-internal: the forwarded-owner assertion (O-04). Every merchant checkout, hand-off record, click and order snapshot is keyed to the partner agent that asked for it. `get_order` and cancel are refused across partners.
- **Merchants see our click id, not the partner.** The attribution map carries no partner name.
- Merchant rate limits are shared across partners, so partner rate isolation is enforced at Pivota's door, before a merchant call.

### D7. Attribution: our click id rides inside the checkout; commission rides the network

- **Every merchant checkout Pivota creates** carries `attribution` with our click id, as `click_id_tag` / `click_id_value` (the pair already used on referral URLs), plus `referring_domain`. Because update is a full replacement, it is re-sent on every update.
- **Where an ACP merchant offers `affiliate_attribution`:** the network provider, our publisher id, and our click id as the sub-id.
- **Labels are not commission.** A checkout created server-side skips the network's tracking link. Per merchant and market, a stored fact records which path carries commission:
  - **network-tracked:** the buyer enters through our tracking link, with our click id as the network sub-id (W8);
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
  - fetch time and a profile fingerprint.

  `execution_routes` is the route identity; the profile facts sit beside it.
- **Pilot eligibility is a stored fact:** the merchant publishes UCP checkout, or ACP. The protocol directory and the doors read it. Nothing is decided by a request-time fetch.
- **The purchasability gate keeps its role:** can this merchant sell to this buyer's market.

### D9. Switches

Every new behaviour defaults off. With its flag off, every answer is byte-identical to today's. Proposed flags:

| Flag | Repo | Effect |
|---|---|---|
| `AGENT_CHECKOUT_UCP_MERCHANT_CHECKOUT_ENABLED` | gateway | the escalation door creates a merchant **checkout** (not a cart) and returns its `continue_url`, totals and messages |
| `MERCHANT_PROTOCOL_PROFILE_SWEEP_ENABLED` | backend job | the D8 capability sweep |
| `UCP_BUYER_AGENT_ORDER_CAPABILITY_ENABLED` | gateway | our platform profile requests the order capability and declares `webhook_url` |
| `UCP_ORDER_WEBHOOK_PERSIST_ENABLED` | gateway + backend | verified merchant order events are stored and served to `get_order` |
| `CARD_LAYER_PARTNER_HANDOFF_ENABLED` | backend | phase-1 hand-off of a purchase to a card-layer partner's agent |
| `UCP_COMPLETE_CHECKOUT_ENABLED` + a per-merchant allowlist | gateway | phase 2 only, after its amendment |

- The existing `MERCHANT_PURCHASABILITY_GATE_ENABLED` and the agent-checkout kill switch apply to every one of them.

### D10. Reuse, freeze, retire

| Component | Decision |
|---|---|
| Gateway UCP buyer client (discovery, negotiation, signing, trust tiers) | **Reuse**: the one transport (D2) |
| Escalation door and `esc_` ids; O-02 hand-off record | **Reuse**: an `esc_` id names a merchant checkout. The merchant checkout id and `continue_url` live server-side in the hand-off record, never inside the `esc_` id, and are never logged |
| Backend `build_attribution` | **Reuse**: moved behind the gateway call, or its field rules ported |
| `execution_routes`, store-audit probe | **Reuse**: route identity; the D8 sweep extends it |
| Merchant purchasability gate; partner identity; partner rate isolation; affiliate sub-id | **Keep** |
| UCP order-webhook receiver | **Reuse and harden** (D4) |
| Visa VDP client, trusted-agent verifier | **Keep** for phase 2 and for recognition |
| Lane A: `orders`, processor router, hosted page, payment finalizer | **Frozen** for connected merchants; hosted links stay off; no new builds |
| In-process ACP lane, AP2 routes | **Frozen dark** |
| Purchase router, parent ledger, Reap agentic rail, A2A token rail, VIC order step | **Frozen**. Phase 1's card-layer hand-off reuses the parent ledger's id and states only if they fit "the partner's agent completes at the merchant"; otherwise a new, smaller record. Retire the remainder once phase 1 runs |
| Pivota-issued cards (`/agent/v1/cards`), refused in production | **Frozen**. Its rule "the card is capped by the merchant's own quote" carries into phase 2 |
| Settlement and transfer jobs | **Paused**; retire with the affiliate payout plan |

## Each flow: today vs protocol-native

| Flow | Today | Phase 1 | Phase 2 |
|---|---|---|---|
| Discovery | Our catalogue; merchant `/.well-known/ucp` fetched at request time, in-memory cache; version pinned | Stored per-domain profile facts (D8); negotiated version | same |
| Merchant decision | Purchasability gate; directory from cart-link and purchasability tables | + "publishes UCP checkout / ACP" fact; directory reads it | + "accepts our credential type; grants complete to us" |
| Checkout URL | `/r` link or a merchant **cart** `continue_url` | Merchant **checkout** `continue_url`, merchant totals, our attribution inside | same, plus completion where allowed |
| Payment completion | Hosted page (off), rails (dark), or buyer on storefront | Buyer at `continue_url`, or a card-layer partner's agent at the merchant's door | Pivota submits the network token / delegated token in the merchant's checkout |
| Order status | None after hand-off; `esc_` not found | Merchant webhooks / `get_order` where granted; partner report; affiliate report | same |
| Cancel / after-sales | Cancel flips our row; refunds refused | `cancel_checkout` at merchant; adjustments surfaced; merchant refunds | same |

## What Pivota signs and holds

- **Signs:**
  - its platform profile (published keys);
  - every request to a merchant door (RFC 9421, under that profile);
  - in phase 2, the buyer's confirmation record (and an AP2 checkout mandate where the merchant uses one).
- **Holds:**
  - merchant capability facts;
  - the hand-off record (partner, click id, merchant, merchant checkout id, `continue_url`, status, source, times);
  - verified order snapshots without buyer contact fields.

  The merchant checkout id and `continue_url` are bearer-like and are kept out of logs.
- **Never holds:**
  - funds;
  - a raw card number;
  - in phase 1, any payment credential;
  - in phase 2, a credential beyond one attempt in memory.

## Phases

1. **Census.** Read-only: what pilot merchants publish today (D8 facts, first from stored evidence, then the sweep).
2. **Merchant checkout + hand-off.** Merchant checkout `continue_url` with merchant totals; `esc_` → hand-off record; `cancel_checkout`.
3. **Order leg.** Order capability requested, webhook persisted, `get_order` on `esc_` from sources.
4. **Card-layer hand-off.** A card-layer partner's agent completes at the merchant, reporting checkout and order ids back.
5. **ACP adapter.** Only if the census finds ACP merchants and they issue us credentials.
6. **Phase-2 completion.** After the amendment in D3, per merchant.

## Risks

- **Commission leakage.** A protocol checkout bypasses network tracking. Revenue depends on the per-merchant attribution fact (D7) and on networks crediting protocol attribution, which is unproven.
- **Completion grants.** A large merchant platform's complete grant is not publicly obtainable, so phase 2 may cover few merchants for a long time.
- **Order visibility.** Merchant webhooks may need per-platform partner onboarding. Until then the hand-off lane shows `handed_off` only.
- **Card-layer partner fit.** A partner whose agent cannot speak UCP or ACP falls back to a browser route, which is out of scope here (ADR-028 D5 still governs it).
- **Version drift.** Merchants move spec versions without notice; negotiation and stored versions replace the pin.
- **Abandoned checkouts.** Creating a merchant checkout before the buyer is ready creates abandoned checkouts in merchant admin. Create on buyer intent only (never on search, preview or probe).

## Open spec questions

1. Whether a checkout created by one platform may be completed by another (the phase-1 rail case). Assumed **no**.
2. The sub-agent or partner identifier inside one platform profile: undefined.
3. Webhook fan-out from a platform to its downstream partners: undefined. Pivota serves it through `get_order`.
4. A handler that names a network agent token, and whether a merchant's payment provider accepts it as `network_token`: per merchant.
5. ACP: how the order webhook URL and the shared secret are provisioned (bilateral), and whether `affiliate_attribution` leaves "Proposal".
6. AP2 inside UCP: whether Pivota may sign as the trusted platform for its partners' users.

## Questions for the owner

1. **First milestone.** Phase 1 step 2: merchant checkout `continue_url` with merchant totals, buyer pays at the merchant. OK as the first build?
2. **Order capability.** OK to request the order capability and declare a `webhook_url` in our public platform profile? This creates a new inbound data flow of merchant order events.
3. **Rail contract.** In the phase-1 rail case, does the card-layer partner's agent create the checkout itself (this ADR's assumption), and does it report the merchant order id back to us?
4. **Lane A.** Frozen as-is for connected merchants, or scheduled for retirement?

## Consequences

- **Fewer money defects.** New money behaviour moves to the merchant's checkout, and most open money defects stop growing.
- **Partners get the merchant's real total** in the agent before the buyer leaves.
- **Order status depends on merchants and card-layer partners,** not on our own state machine.
- **Revenue needs a per-merchant attribution fact,** and that has to be proved order by order.

## Alternatives considered

- **Keep building lanes A and B.** Rejected by the owner on 2026-11-10: the defects are in custom plumbing.
- **Pivota completes from day one.** Blocked by completion grants, by credential custody, and by the card-network enrolment. Kept as phase 2.
- **Per-partner platform profiles.** Rejected for now (D6).
- **The browser route as the default completion.** Out of scope. It remains governed by ADR-028 D5.
