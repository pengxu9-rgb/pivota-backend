# ADR-028: Pivota completes the order on the merchant's own checkout; the buyer stays in the agent front

**Status:** Proposed, 2026-11-02. peng decided the direction and five questions on 2026-11-02 (see "Resolved questions").

**Decision owner:** peng

**Builds on:**
- ADR-014 (agent-protocol interoperability): each completion protocol, and each card network's agent program, is an edge adapter on one spine.
- ADR-016 (Pivota is non-custodial): unchanged. The merchant's payment provider charges; Pivota never holds funds, takes no cut of them and is never the merchant of record.
- ADR-012 (identity and consent): the buyer's consent and its spending ceiling bound every completed order.
- ADR-025 (the attribution loop): every completed order carries our click id; commission comes from attribution, never from the payment.
- ADR-027 (deals): codes are entered in the completed checkout. Amendment 3 of ADR-027, in the same change as this ADR, moves its main lane here.
- ADR-009 (seller of record) and ADR-026 (a market is data): completion is decided per seller and market.

## Context

Today a recommendation ends in a click-out: the `/r` redirect sends the buyer to the merchant's product page or a pre-filled cart, and the buyer finishes there, mostly as a guest.
- The buyer leaves the agent front at the moment of decision. A buyer who goes looking for a coupon may not come back (ADR-027 Q11).
- Attribution depends on what the buyer's own browser carries to the merchant.
- Pivota cannot show the merchant's final total, with discounts, shipping and tax, before the buyer commits.

peng decided on 2026-11-02 that the buyer checks out in the agent front, and that Pivota completes the order on the merchant's own storefront checkout, with an agent payment token. The buyer is sent to the merchant only when no offer for that product can be completed by Pivota.

### What exists and can be reused
- **A protocol client.** The gateway's UCP buyer-agent client discovers a merchant's `/.well-known/ucp` profile, creates carts and checkouts and reads their totals. It signs requests at UCP's signed trust tier with a key published in our agent profile. Today it never calls `complete_checkout`, by design.
- **A protocol adapter framework** (ADR-014) where Visa's Trusted Agent Protocol and Mastercard Agent Pay are already named as future adapters. A Trusted Agent Protocol verifier exists in the gateway in observe-only mode.
- **The orchestrated checkout** for connected merchants: the merchant's payment provider charges and Pivota orchestrates (ADR-016).
- **Card-network enrolment** for agent payment credentials (Visa Intelligent Commerce) has started; its open decisions are tracked outside this ADR.

### What does not exist
- Completing an order on a merchant that has not connected to Pivota.
- An agent payment token in any order flow.
- A browser runner that completes a storefront checkout.
- Attribution on an order Pivota completes, rather than one the buyer completes in their own browser.

## Decision

### D1. The completed order is the product; a click-out is the fallback

- An offer is **completable** when Pivota holds a stored, current fact that at least one completion route (D2) works for that seller and market. The fact comes from probes and from completed orders, never from a guess at request time.
- **Serving.** For a product with at least one completable offer, click-out offers for that product are not recommended. A completable offer always ranks above a click-out offer for the same product, **even at a higher price** (peng, Q2 below). Among completable offers, the buyer's price comes first (ADR-027 Q6).
- Whether an offer carries our attribution never affects its rank (ADR-025 Q4): an offer completed without commission (D4) ranks like any other completable offer.
- A product with no completable offer is served as today, with click-outs.
- **At completion time**, a route that fails before payment (merchant blocks the agent, item unavailable, token declined) falls back to the next completable offer for the product. Only when none remains is the buyer offered a click-out.

### D2. Two completion routes, chosen per merchant by which one carries our attribution

- **Protocol route:** the merchant's agentic checkout protocol, at the trust tier the merchant requires: UCP `complete_checkout`, ACP, and others as ADR-014 adapters. Payment goes through the protocol's payment handler.
- **Browser route:** a headless browser completes the merchant's storefront checkout, paying with an agent payment token from a card network's agent program (Visa Intelligent Commerce, Mastercard Agent Pay).
- **Choosing a route,** per seller and market:
  1. a route that carries our authorized affiliate attribution (D3). When both do, the protocol route;
  2. otherwise, any route that completes, without commission (D4).
- Which routes work, and which carry attribution, is a stored fact per seller and market, refreshed by probes that never pay and by the outcome of real orders.

### D3. Attribution: our authorized tracking travels in the session that completes the order

- **Affiliate networks we have joined.** On the browser route, the session enters the merchant through the network's tracking link generated for our account, with our order reference as the sub-id. The network's click id therefore lands in the same session that completes the order, and the network credits it as it would any buyer's order.
  - Only for programs whose terms, or the merchant in writing, allow orders an agent places on a buyer's behalf. That permission is recorded per program, like ADR-027 Q13.
  - Exactly one tracking click per buyer checkout. Never a tracking click without a buyer's order attempt behind it, and never to refresh a cookie.
  - The deal code and the tracking arrive together (ADR-027 Q12).
- **Protocol route.** Attribution counts only where the protocol or the network carries it to the order: an attribution field the merchant records, or a network conversion interface that accepts our click id. It is checked per merchant or program before a route is marked as carrying attribution.
- **Agentic affiliate networks:** treated the same way. A recorded program with terms that allow agent-placed orders.
- Every completed order carries our click id and order reference (ADR-025), whatever pays commission on it.
- The first orders on each program are checked by hand: the network's report must show our sub-id on them. A program that shows a miss goes back to "no attribution" (D4) until a person reviews it.

### D4. No route carries attribution: complete the order, then fix the loop

- The buyer's order comes first: Pivota completes it without commission (peng, Q1 below).
- The order is recorded as **unattributed**, with why: no program for this merchant, the program does not allow agent-placed orders, the route drops the tracking, or the network did not credit it.
- Each unattributed order creates a task for a Pivota employee to fix the commission loop for that merchant: join its program, ask for permission for agent-placed orders, or set up conversion reporting. The task names the merchant, the market and the reason, and stays open until the loop is fixed or a person closes it.
- Metric: unattributed completed orders per merchant, reviewed weekly.

### D5. Identity: a recognized agent, never evasion

- Every completion request is signed with Pivota's agent identity: protocol signatures (UCP signed and token tiers) and, on the browser route, the card networks' agent recognition (Trusted Agent Protocol, Agent Pay, HTTP message signatures).
- The browser route runs only on merchants whose checkout accepts our recognized agent.
  - On any CAPTCHA, bot challenge or block, it stops. That merchant becomes not completable on that route until a person reviews it.
  - It never solves a CAPTCHA, rotates addresses, or disguises its user agent or fingerprint.
- A merchant that opts out is never completed on either route.

### D6. Money and card data: non-custodial, tokens only

- ADR-016 holds as written. The merchant's payment provider charges an agent payment token. Pivota never holds funds, takes no cut of them, and is not the merchant of record.
- No raw card number enters a Pivota system. Payment credentials are the card network's agent tokens, issued under its agent program.
- How a credential reaches the merchant's checkout (a protocol payment handler, or entry into the checkout's payment fields by the browser) is decided with the card-network enrolment. The card-data scope of each path is assessed before that path takes a payment.
- The buyer's consent bounds every order: its spending ceiling (ADR-012) and an explicit confirmation of the final total (D7).

### D7. The buyer confirms the merchant's own final total, in the agent front

- Before any payment, the agent front shows what the merchant's checkout itself returned: items, the deal code and any automatic discounts, shipping, tax and total. The buyer confirms that total; only then is the order paid.
- If the merchant's total changes after confirmation, the buyer confirms again.
- The buyer's contact and delivery details are entered in the merchant's checkout only with the buyer's explicit consent for that order.
- The merchant sends its own order confirmation. After-sales requests go through Pivota's existing after-sales flow to the merchant.

### D8. Deal codes on this lane

- Pivota enters the best published, verified code in the checkout it completes (ADR-027 amendment 3, Q15). The merchant's checkout result, before payment, is the evidence: the buyer sees it in D7's confirmation.
- A rejected code is removed and the buyer sees the price without it. Nothing is paid on a price the checkout did not show.

### D9. Switches and failure handling

- Flags, all default off: completion at all; each route; each market. The browser route starts on an allowlist of merchants.
- D1's serving rule is a separate flag, switched on per market only after the completion success rate has been measured, so recommendations never prefer offers that cannot actually be completed.
- After a payment is authorized, the merchant's order state is the truth. A payment is never retried blindly: each attempt is idempotent and keyed by our order reference.
- Every attempt records the route, the outcome and, on failure, the reason, so the "completable" facts (D1) and the route choice (D2) learn from real orders.

## Phases

- **Phase A, one route, one pilot merchant, one market.**
  - Protocol route where a pilot merchant's protocol supports completion at a tier we hold.
  - Or the browser route once the card-network enrolment gives us an agent token and a credential path.
  - Orders are placed by Pivota staff as real buyers. Each is checked by hand: total confirmed, code applied, attribution present, merchant order received.
- **Phase B, attribution.** Per program: permission for agent-placed orders recorded, first orders checked in the network's report (D3). The unattributed-order task flow (D4) is live before the first unattributed order.
- **Phase C, serving.** D1's serving rule per market, after the completion success rate has been measured.

## Open dependencies (facts and enrolments, not decisions)

- The card-network enrolment: our role in each agent program, and the credential path (D6).
- Per program: whether agent-placed orders are allowed, and whether the network reports our sub-id on them (D3).
- Per protocol: whether completion carries attribution, and at which trust tier completion is allowed.
- Which merchants' checkouts accept a recognized agent on the browser route (D5).

## Consequences

- Recommendations change: completable offers win over click-outs for the same product, including at a higher price.
- `/r` click-outs become the fallback for products with no completable offer.
- Attribution moves from the buyer's browser into the session Pivota completes. That makes it observable per order, and makes per-program permission for agent-placed orders a hard requirement.
- A new operations loop: unattributed orders become tasks to fix the merchant's commission loop.
- Card-data scope grows with the credential path, and is assessed per path before it takes payments.
- Coverage depends on merchants recognizing agents. Where they do not, the product falls back to click-outs; Pivota never forces its way through.

## Alternatives considered

- **Keep click-outs as the main lane.** Rejected: the buyer leaves at the moment of decision, and code coverage before a guest checkout is the conversion risk ADR-027 Q11 names.
- **Protocol route only.** Rejected as the only route: too few merchants support completion today. It stays the preferred route where it carries attribution.
- **A browser route that works around bot detection.** Rejected: it would impersonate a person and evade the merchant's controls. Only recognized, signed agents.
- **Pivota as merchant of record, or holding funds.** Rejected by ADR-016.

## Resolved questions (peng, 2026-11-02)

1. **No route carries attribution:** complete the order without commission first, then notify a Pivota employee to fix the commission loop (D4).
2. **A completable offer costs more than a click-out offer for the same product:** the completable offer always wins (D1). This overrides ADR-027 Q6 between lanes; within completable offers, the buyer's price still comes first.
3. **Commission:** through affiliate networks we join, with our authorized tracking in the checkout session, or through agentic affiliate networks (D3).
4. **Routes:** consider both the protocol route and the browser route, whichever is available, provided it can carry our attribution; when neither can, Q1 applies (D2).
5. **Leaving the agent front:** the buyer is not sent to the merchant unless no offer for the product can be completed by Pivota. Where an agentic offer exists, click-outs are not recommended (D1).
