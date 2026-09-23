# ADR-025: The attribution loop: agent → click → order evidence → commission → agent share

- **Status:** Proposed, 2026-09-23
- **Scope:** every lane where Pivota puts a buyer in front of a merchant. That covers referral
  links, cart links, UCP checkout, and Reap. No specific affiliate network is assumed.
- **Relates to:** ADR-017 (its "Option C" is a different, rejected option), ADR-009 §D3 (seller-keyed
  edges), `services/commerce_attribution_service.py`, `services/merchant_ucp_checkout.build_attribution`

## The loop

Pivota earns on a sale, and pays the agent that originated it, only if all six links hold:

```
1 agent identity → 2 click minted (agent on it) → 3 click id carried to the merchant
  → 4 order evidence comes BACK with that id → 5 commission recorded → 6 agent share accrued
```

Link 4 is the one that decides everything. Pivota never sees a merchant's orders unless someone
reports them. Whether that someone is an affiliate network, a connected store, a merchant
statement, or a checkout partner, it only has to hand back our click id. The design below is
built around making link 4 exist for every merchant we send buyers to.

## Measured state, prod, 2026-09-23

Read-only one-off jobs. The programs and their output are in the author's local `reports/attribution_chain_2026_09_23/` and are not committed, following this repo's practice for reports.

| Link | Measurement | Verdict |
|---|---|---|
| 1 agent | 26 agents, 15 API keys | exists |
| 2 click minted | `surface_click_events`: **18 rows ever**, 7 with a non-empty `agent_id` (at least one is the `'unknown'` sentinel `traffic_taxonomy_service` writes when there is no agent), last 2026-08-27, 1 in the last 30 days | effectively not running |
| 2 | `offers.resolve` mints a click id per offer (`agent_shop_gateway.py:4748`), but the row is written **only when a buyer hits `/r`**. The ctx it signs carries no agent id. An agent that hands out the published `pdp_url`/`cart_url` directly produces no click row at all. | gap |
| 2 | `outbound_click_events`: 46 ever, 1 in the last 30 days (a script UA). `agent_product_events` over 30 days: 188 impressions, 7 clicks. | we cannot see click-outs |
| 3 carried | Referral URLs carry `pvt_click_id` as a query param, cart links carry `attributes[pivota_click_id]`, the UCP stamp (`build_attribution`) has **no production caller**, and Reap uses a cart attribute. The warm handoff (`outbound_warm_handoff.py`, off by default) also sends `attribution: {pivota_click_id}` on the merchant's UCP `create_cart`. | carried, but mostly to merchants who never read it |
| 4 evidence | Closure sources that exist: Shopify `orders/paid` webhook and poller, the WooCommerce poller, the self-report API, and a Reap report. **All four need a connected merchant or a partner.** Connected stores: 17 Shopify + 4 Wix (`merchant_stores`), versus **328 checkout hosts** in the serving catalog. There is no affiliate-network import. | **the break** |
| 4 | `commerce_attribution_edges`: 15 rows, all one merchant (the test store), 2026-03-30 to 07-10. **0 have `converted_at`**, and 0 carry an external order id. | the loop has never closed a real order |
| 5 commission | `gmv_attribution_daily`: 2 rows (test store, March), $175 GMV, 5% take. Real invoices: all $0. The take is computed as a flat 10%/5% per merchant (`gmv_aggregation_service.py:149`). A network-paid commission has nowhere to land. | model mismatch |
| 6 agent share | `agent_payouts` holds only channel-partner rows, several with test-dated periods (2036/2047), some failed with "insufficient platform balance". The legacy `commissions`/`merchant_commission_offers` system was retired 2026-05-23. | no agent accrual path |

**Conclusion:** the attribution machinery exists as parts, but it has never carried one real
order end to end. The reason isn't a missing affiliate program. For almost every merchant we send
buyers to, **nothing ever reports an order back** (link 4), and on the lanes agents actually
use, the click is never recorded with its agent (link 2).

## Decision

### D1. The click is minted and recorded when the link is issued, with the agent on it

Every URL, cart, or checkout Pivota hands to an agent is issued against a `surface_click_events`
row written **at issue time**. The row records `agent_id` (from the authenticated caller, never from
the body), the seller, product, and lane, with state `issued`. A later `/r` hit increments
`click_count`; it no longer creates the row. That makes the agent recoverable even when the agent
hands the buyer our direct `pdp_url`/`cart_url` instead of `/r`. It also gives us an honest
"links issued vs clicked" number, which today reads as zero.

One helper (`issue_click`) is the only way to mint. All lanes call it.

### D2. The click id travels in whatever form that merchant's evidence source reads back

A per-seller **evidence route** decides the carrier:

| Evidence source (link 4) | Carrier (link 3) | Reads back |
|---|---|---|
| Affiliate network (any: Rakuten, Impact, Awin, CJ, …) | the network's tracking link, with our click id in its **sub-id** parameter | the network's transaction/commission report, keyed by sub-id |
| Connected store (Shopify/Woo/Wix) | `attributes[pivota_click_id]` cart attribute, or the `pvt_click_id` param | webhook / poller `note_attributes` (exists) |
| Direct agreement, merchant statement | `pvt_click_id` param / cart attribute / UCP stamp | monthly order report through the self-report API (exists) |
| UCP agentic checkout | UCP `attribution` member (`click_id_value`) | the merchant's order snapshot, via connection or statement |
| Reap agentic | cart attribute + Reap order report | `partner_reported` (exists, dark) |

A seller with **no evidence route** is still served, but it is recorded as `unattributable`.
Nobody should mistake traffic to it for revenue. The coverage census
(`scripts/affiliate_coverage_census.py`) is the tool that measures how much of the catalog
falls there. Rakuten is its first adapter, not its premise. When D2's network link wrapping lands, the warm-handoff affiliate denylist (`outbound_warm_handoff.AFFILIATE_HOST_SUFFIXES`) must cover every wrapper host it emits. Today it misses CJ's (anrdoezrs.net, jdoqocy.com, ...) and Impact vanity domains.

### D3. Evidence adapters share one contract

Every source normalises to a single call:
`close_external_order_conversion(merchant_id, click_id, external_order_id, gross, currency,
converted_at, source, reported_commission?)`. It stays idempotent on (merchant, order), as today.
A network adapter is a scheduled job: pull transactions since the watermark, map sub-id to
click id, close. Adding a network means adding an adapter, not a new pipeline.

### D4. Commission is recorded as reported when a source reports it

A network or agreement statement says what the merchant actually paid. That amount is stored
on the edge (`reported_commission_cents`, `commission_source`). The flat take rate applies only
where no source reports an amount. Reversals (network locks, returns) update the edge.

### D5. The agent's share accrues from closed edges

A closed edge whose click carries `agent_id` accrues that agent's share under a per-agent rate
schedule (stream `attributed_commission`). Accrual is bookkeeping. **Paying it out is an open
decision**, because of the 2026-09-06 rule that Pivota does not hold or move money. The existing
payout rail already failed on "insufficient platform balance", which is that constraint showing up.

### D6. The primary lane: a payment partner completes the UCP checkout with an agentic token

Most orders are expected to run like the Reap integration. Pivota creates a checkout session
at a **payment partner** (Reap today; any Visa Intelligent Commerce / Mastercard Agent Pay
partner later). The partner completes the purchase at the merchant with the buyer's agentic
token, and Pivota learns the outcome from **the partner's checkout session**. Pivota does not
integrate with the merchant.

- **Link 2** is our own call. The agent is known from the authenticated caller when Pivota
  creates the purchase (`reap_agentic_purchases.agent_id`). No click is needed.
- **Link 4, order confirmation, works this way already:** Pivota polls
  `GET /agentic/checkouts/{id}` until `COMPLETED` with an `orderId`, then closes the edge
  `partner_reported` (`reap_agentic_client.get_checkout`, `checkout_state`;
  `reap_agentic_purchase.py`). Agentic resources have no webhooks, so polling is the only
  channel.
- **Link 4, updates after the order, has NO channel today.** The partner's checkout states end
  at `COMPLETED | FAILED | EXPIRED`, and the poller stops at terminal. Refunds, cancellations,
  returns, chargebacks and partial fulfilment are never seen, so commission can't be clawed back
  and an agent's share could be paid on a refunded order.
- **Link 3 has no carrier to the merchant.** Reap's API drops unknown keys and has no
  attribution field. The only join is our `returnUrl` query string. The merchant's order shows
  the partner, not Pivota.

**Reap, verified against docs.reap.global on 2026-09-23** (OpenAPI + agentic-payments pages):

- `GET /agentic/checkouts/{id}` returns only `id, status, quoteId, enrollmentId, orderId,
  finalAmount, nextAction, createdAt, updatedAt`. Status is `REQUIRES_ACTION | PROCESSING |
  COMPLETED | FAILED | EXPIRED`: **there is no state after COMPLETED**, so a refund or
  cancellation cannot appear there even by polling.
- "Reap does not send webhook events for agentic resources. Poll Get checkout." Checkout,
  enrollment and mandate webhooks are **on Reap's roadmap**, as is "order verification that
  matches each order to the card authorization on Reap's rails".
- `POST /agentic/checkouts` takes only `quoteId, enrollmentId, presentation.returnUrl`. There is
  **no metadata, external reference or attribution field**; `returnUrl` is the only join.
- Reap **does** have signed webhooks (HMAC-SHA256, `X-Reap-Webhook-*` headers, at-least-once,
  unordered, retried for ~48h). Its `CARD_TRANSACTION_UPDATED` (clearing, reversal, refund) and
  `CARD_DISPUTE_STATUS_UPDATED` events carry exactly the post-order money truth. But they fire only
  for cards issued in the webhook owner's own card program. Pivota runs no card program (the
  2026-09-06 rule), and today the buyer enters any Visa card on the hosted page. So those events
  are not ours to receive, and nothing links a checkout to a card transaction.
- Agentic Payments is not generally available. Today every purchase is approved per purchase on
  the hosted page; card vaulting and passkey mandates are on the roadmap. Reap also plans to
  register "the agent execution identity with the relevant agent registries" so merchants can
  verify agent traffic.

So on this lane today: confirmation = poll to COMPLETED (works); everything after = nothing.
The asks below map onto Reap's own roadmap items, plus two that are not on it: states after COMPLETED (item 1) and the attribution pass-through (item 4).

**What Pivota asks the payment partner for** (contract / API requests, the same list for every
partner):

1. **Post-completion order events** on the checkout, keyed by the partner's checkout id and our
   reference: refunded (full/partial, amount), cancelled, returned, disputed/charged back,
   fulfilment status. Either a signed webhook to Pivota, or an `events`/`adjustments` array on
   `GET /agentic/checkouts/{id}` that we keep polling through the return window.
2. **Token / payment events.** As the party holding the agentic token, the partner sees the
   card-network lifecycle: authorisation, capture/settled amount, reversal, refund credit,
   chargeback. That is the money truth of the order, independent of the merchant.
3. **Forward the merchant's UCP order events.** When the partner completes through the
   merchant's UCP checkout, the partner is the negotiated platform. UCP lets it declare
   `dev.ucp.shopping.order` with a `webhook_url`, and merchants then push signed (RFC 9421)
   lifecycle snapshots with `id` + `checkout_id`. The partner forwards those to us.
4. **An attribution pass-through.** A field on checkout create that the partner copies into the
   merchant's UCP `attribution` member (referrer `pivota`, our click id), so the merchant's own
   order shows Pivota as the originator. That is what any merchant-side commission rests on.
5. **Revenue terms.** Who pays Pivota for an originated order: a partner revenue share (e.g. on
   the token/issuing economics), or a merchant fee evidenced by item 4. Protocol mechanics do not
   decide this; the contract does.

Pivota's side: keep polling after `COMPLETED` at a slow cadence for the return window once
item 1 exists; store every event against the purchase; update the edge (refund → reversal of
commission and of the agent's accrual).

**Measured 2026-09-23:** prod `reap_agentic_purchases` = **0 rows**. A completed checkout has
been reported to Peng (plan note, 09-23), but it did not pass through Pivota's purchase ledger,
so no edge exists for it.

## Proof gate

Nothing counts as working until **one real order per evidence route** has passed all six links
in prod: issued click with `agent_id` → buyer purchase → evidence arrives → edge `converted` with
the agent resolved → commission recorded → agent accrual line. Status labels follow the run, not
the code.

## Build order

Payment-partner lane first (D6), because that is where the orders will be:

- **P1.** Send the partner the D6 asks (items 1–5). They gate everything below them.
- **P2.** Route every agentic purchase through the purchase ledger (agent from auth), so a
  completed checkout always yields a `partner_reported` edge carrying `agent_id`.
- **P3.** Post-completion polling / webhook receiver for partner events. Refunds and chargebacks
  reverse the commission and the agent accrual.
- **P4.** Proof order: one real partner-completed order through all six links, including one
  refund.

Status, 2026-09-23:
- **P1** is drafted for Peng to send (not sent).
- **P2:** #2267 (open at the time of writing) makes a completed purchase's edge carry the purchase's authenticated `agent_id`.
  Before it, the variant lane closed with no agent, because it writes no click row. The lane
  itself is still dark: prod `reap_agentic_purchases` = 0 rows.
- **P4's measuring tool** is #2268 (open at the time of writing), `scripts/agent_attribution_funnel.py`: per-agent issued →
  clicked, opened → completed → credited, plus the integrity exceptions. Its prod baseline over
  30 days is 1 link issued, 0 purchases, 0 credited.

Then the referral lanes:

1. **`issue_click` at issue time with `agent_id`** (offers.resolve, `/r`, the Reap cart link,
   and UCP create). Add an "issued vs clicked vs converted" funnel query. Nothing else is
   measurable until this ships.
2. **Evidence-route registry per seller**, with network sub-id link wrapping in
   `compose_attributed_destinations`. Only a human-clicked `/r` gets the network link; bots get
   the unwrapped URL, because prefetchers setting cookies is cookie stuffing.
3. **First network adapter** (transactions by sub-id → closure) and the reported-commission
   fields.
4. **Proof run** on one network merchant and one connected store.
5. **Agent accrual** and an agent-facing statement.
6. **Agentic lanes**: a UCP stamp caller, the gateway injecting the stamp instead of dropping it,
   and Reap arming. These wait on 1–3, because they close through the same evidence contract.

## Open questions (owner: Peng)

1. **Agent payouts:** is paying agents out of commission Pivota receives within the no-money
   rule, or do agents get statements only?
2. **Merchant visibility:** do merchants and networks see only Pivota, or also which agent
   originated an order? This record assumes only Pivota (click id only).
3. **Revenue source on the partner lane:** a share from the payment partner, a merchant fee, or
   both. This decides whether item 4 (the merchant sees Pivota) is required or only nice to have.
4. **Unattributable sellers:** do we keep serving sellers with no evidence route at the same
   rank, or prefer attributable sellers when products are equivalent?
