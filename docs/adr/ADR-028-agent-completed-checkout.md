# ADR-028: Pivota completes the order on the merchant's own checkout; the buyer stays in the agent front

**Status:** Proposed, 2026-11-02. peng decided the direction and five questions on 2026-11-02 (see "Resolved questions"). Revised 2026-11-02 after review. One owner decision is open (D5, robots and terms). **Amended 2026-11-08 (amendment 1)** with three decisions of the decision owner: D6 is replaced (Pivota never sees, receives or holds the card credential; the payment partner fills the merchant's checkout), and D10 (the merchant is merchant of record on every lane) and D11 (a refund is a request to the merchant) are added. See "Amendment 1" below.

**Decision owner:** peng

**Builds on:**
- ADR-014 (agent-protocol interoperability): each completion protocol, and each card network's agent program, is an edge adapter on one spine.
- ADR-016 (Pivota is non-custodial): unchanged and binding (D6).
- ADR-012-agent-transaction-rail-boundary (identity and consent): the buyer's consent and its spending ceiling bound every completed order (D7).
- ADR-025 (the attribution loop): our click id travels as the sub-id (D2 there); commission comes from attribution, never from the payment. **This ADR supersedes ADR-025 D6's statement of the primary lane.** The primary lane is the completed checkout defined here. A payment partner's lanes (ADR-025 D6a and D6b) are routes under D2 when they meet every rule in D3 to D9.
- ADR-027 (deals): codes are entered in the completed checkout. Amendment 3 of ADR-027, in the same change as this ADR, moves its main lane here.
- ADR-009 (seller of record) and ADR-026 (a market is data): completion is decided per seller and market.

## Amendment 1 (2026-11-08): the credential, the merchant of record, refunds

The decision owner decided three questions on 2026-11-08. They are binding on every lane, not only on the completed checkout.

1. **The card credential (D6, replaced).** The payment partner fills the merchant's checkout. Pivota never sees, receives or holds the card credential: no card number (PAN), no device or network token number (DPAN), no cryptogram, no dynamic security code (dCVV), in any Pivota process, log or store.
2. **The merchant of record (D10, new).** The merchant is the merchant of record on every lane, and Pivota is never the payee of a buyer's payment.
3. **Refunds (D11, new).** A refund is a request to the merchant. Pivota never executes one.

**How to read the rest of this ADR after the amendment.** "Pivota completes the order" now means that Pivota prepares the order and the buyer's mandate (D7), dispatches it on a route (D2), and reads its outcome. The step that carries the credential belongs to the payment partner, or to the protocol's payment handler, under the conditions in D6. The amendment changes these passages, marked in place: the Context, D2 (both routes), D6, D9 (refunds), Phase A, the open dependencies, the consequences, and the alternatives.

**Consequences of the amendment**
- **The payment partner's token step stays refused in production.** That step returns a network token and cryptogram to the caller. It MUST NOT be enabled in any environment that can take a real payment, and it is to become a refusal in code rather than a switch. The partner is asked to stop sending credential fields to Pivota's account at all.
- **Pivota's own credential retrieval from a card network is out of scope.** No Pivota code calls a card network's credential endpoint (for Visa Intelligent Commerce, `credentials-for-instruction` or its predecessor), whether the answer is readable or encrypted. Card-network connectivity work stops before any such call. Reopening it needs a new decision that amends D6.
- **Agent programs that expect the agent to enter the token** in the checkout's payment fields are usable only through a payment partner that does it on its own side.
- **Refunds are request-only** on every lane (D11). An after-sales tool or API that executes a refund on a merchant's payment provider is a contradiction to remove (see below).
- **Stripping is a detector, not compliance.** Pivota's clients strip credential-shaped fields from partner answers and count each one as a breach. A credential that reaches the stripper has already entered a Pivota process, so the rule is met only when the partner never sends one. The partner's written confirmation, and a sandbox log scan showing no credential fields, are conditions for a pilot.
- **Card-data scope.** The design keeps card credentials out of Pivota's systems, which is the precondition for the SAQ-A scope. **A scoping opinion from a Qualified Security Assessor (QSA) is still to be obtained**, and no SAQ-A claim relies on this ADR until it is on file. The opinion covers each live route, including the protocol route's payment handler and the redirects to the partner's hosted pages.

**Cross-references**
- **ADR-016 (non-custodial).** Unchanged. D10 restates its money boundary per lane: the merchant's payment provider, or the payment partner acting for the merchant, charges; funds never settle to Pivota.
- **ADR-025 (the attribution loop).** Its D6 partner lanes already have the partner filling the card token into the merchant's checkout (D6b), which is the model D6 now requires. Its link 4 already places refunds on the merchant's side, which D11 makes the rule. Commission and revenue share still settle to Pivota from networks or partners after the sale (ADR-025 D5). That is Pivota's own revenue, not a buyer payment, and D10 does not forbid it.
- **ADR-026 (a market is data).** Payee and currency are per merchant and market. A payment partner order carries the market's currency, and the payee is set per merchant, never by a platform default.
- **ADR-027 (deals).** Its completed-checkout lane says that Pivota enters the code in the checkout it completes. Under D6, a code travels in the cart or checkout Pivota hands to the route (a cart permalink or a protocol checkout's codes), or is entered by the partner. The code rules in D8 and ADR-027 Q14 and Q15 are unchanged.

**Statements elsewhere that now contradict the decisions.** These are listed here and not edited in this change. Each needs its own follow-up.

| Where | Statement | Contradicts |
|---|---|---|
| ADR-027, the lane table row "Pivota-completed merchant checkout" and "The completed-checkout lane" | "Pivota enters the code in the checkout it completes"; "the merchant's payment provider charges an agent payment token" in a checkout Pivota completes | D6: the partner fills the checkout; Pivota does not complete the payment step |
| ADR-012-agent-transaction-rail-boundary, Context, Rail 1 | the transact journey ends "track / refund / cancel" on Pivota's API-key rail | D11 |
| ADR-015, decision 5 and action item 6 | a refund is an action with its own authorization (`action=refund`), aligned with ADR-013's reversal entries | D11: Pivota executes no refund, so what an agent can be authorized for is a refund request |
| `docs/TARGET_ARCHITECTURE.md`, layer table and red line 3 | "Money execution: ... refunds: Live, gated"; "Capture and refunds EXIST, by decision" | D11 |
| `AFTER_SALES_CASE_API_V1.md`, step 5 | `POST /agent/v1/after-sales/cases/{case_id}/refund` "executes" a full or partial refund | D11 |
| `docs/agentic-commerce-price-inventory-audit.md` (transaction-safety notes) and `docs/agentic-commerce-transaction-safety-next-phase.md` | an operator endpoint issues an idempotent PSP refund for a paid order whose merchant order failed; refund recovery adapters per PSP | D11, unless a later decision treats reversing Pivota's own failed orchestration as a separate case |
| ADR-028 D9 (this ADR, superseded in place) | "it executes a refund only where the merchant is connected" | D11 |
| ADR-028 Consequences (this ADR, replaced in place) | "Card-data scope stays SAQ-A by construction (D6)" | the QSA note above |
| ADR-013 (superseded by ADR-016) | a Pivota ledger with refund and reversal entries | D10 and D11; already superseded, listed for completeness |

Code that holds a contradicting position is tracked separately, not in this ADR: the payment partner rail's token step (behind its switch, off), and the card-network client's decrypt-then-strip of a credential answer (no credential call exists).

## Context

Today a recommendation ends in a click-out: the `/r` redirect sends the buyer to the merchant's product page or a pre-filled cart, and the buyer finishes there, mostly as a guest.
- The buyer leaves the agent front at the moment of decision. A buyer who goes looking for a coupon may not come back (ADR-027 Q11).
- Attribution depends on what the buyer's own browser carries to the merchant.
- Pivota cannot show the merchant's final total, with discounts, shipping and tax, before the buyer commits.

peng decided on 2026-11-02 that the buyer checks out in the agent front, and that Pivota completes the order on the merchant's own storefront checkout, with the buyer's agent payment token. The buyer is sent to the merchant only when no offer for that product can be completed by Pivota.

> **Amended 2026-11-08 (D6):** the buyer still checks out in the agent front, but Pivota never handles the agent payment token. The payment partner fills the merchant's checkout with it.

### What exists and can be reused
- **A protocol client.** The gateway's UCP buyer-agent client discovers a merchant's `/.well-known/ucp` profile, creates carts and checkouts and reads their totals. It signs requests at UCP's signed trust tier with a key published in our agent profile. Today it never calls `complete_checkout`, by design.
- **A protocol adapter framework** (ADR-014) where Visa's Trusted Agent Protocol and Mastercard Agent Pay are already named as future adapters. A Trusted Agent Protocol verifier exists in the gateway in observe-only mode.
- **The orchestrated checkout** for connected merchants: the merchant's payment provider charges and Pivota orchestrates (ADR-016).
- **Card-network enrolment** for agent payment credentials is an open dependency (see "Open dependencies").

### What does not exist
- Completing an order on a merchant that has not connected to Pivota.
- An agent payment token in any order flow.
- A browser runner that completes a storefront checkout.
- Attribution on an order Pivota completes, rather than one the buyer completes in their own browser.

## Decision

Every rule below written as **MUST** or **MUST NOT** is enforced in code, behind the flags in D9, and has a test. None is left to convention.

### D1. The completed order is the product; a click-out is the fallback

- An offer is **completable** when Pivota holds a stored, current fact that at least one completion route (D2) works for that seller and market. The fact comes from probes (D5) and from completed orders, never from a guess at request time.
- **Serving, for one product:**
  - when the product has at least one completable offer, click-out offers for **that product** are not recommended. A completable offer ranks above every click-out offer for the same product, **even at a higher price** (peng, Q2 below);
  - among the completable offers for that product, ADR-027 Q6 orders them: the served price, and the verified effective price only once Q6's measurement gate has opened for this lane (ADR-027 Q16);
  - whether an offer carries our attribution never affects its rank (ADR-025 Q4). An offer completed without commission (D4) ranks like any other completable offer;
  - completability orders **sellers within one product** only. It never reorders products in a recommendation slate.
- A product with no completable offer is served as today, with click-outs.
- **Falling back at completion time.** A route that fails **before payment** (the merchant does not recognize the agent, the item is unavailable, the checkout errors) may fall back to the next completable offer for the product. Nothing MUST be paid on that offer before **a new buyer confirmation** of its own total (D7). Only when no completable offer remains is the buyer offered a click-out.
- **A payment decline is not a fallback.** It MUST stop and return to the buyer. The credential MUST NOT be retried at another merchant: retrying one card across merchants is the card-testing pattern.

### D2. Two completion routes, chosen per merchant

- **Protocol route:** the merchant's agentic checkout protocol, at the trust tier the merchant requires: UCP `complete_checkout`, ACP, and others as ADR-014 adapters. Payment goes through the protocol's payment handler.
  - *Amended 2026-11-08 (D6):* only where no card credential passes through Pivota on the way to the handler. A handler that needs Pivota to carry a card number, a network token, a cryptogram or a dynamic security code is not used.
- **Browser route:** a headless browser, ours or a payment partner's, completes the merchant's storefront checkout, paying with the buyer's agent payment token from a card network's agent program (Visa Intelligent Commerce, Mastercard Agent Pay).
  - *Amended 2026-11-08 (D6):* the browser route is the **payment partner's** runner. It fills the merchant's checkout and pays with the credential on the partner's side. Pivota's own runner does not complete a checkout and never enters a credential.
  - A payment partner's runner cannot be held to Pivota's code. Pivota MUST NOT dispatch an order to a partner's browser runner unless the partner's contractual equivalents of D5 to D7 (identity, challenges, guest checkout, card data, the mandate), D10 (the payee) and D11 (refund requests) are recorded for that partner.
- **Choosing a route,** per seller and market (review rule, pending owner acceptance; refines Q4):
  1. the route with the **lower buyer total** wins. Routes can differ, for example in shipping options or in which codes they accept;
  2. at an **equal buyer total**, a route that carries our authorized attribution (D3); when both do, the protocol route;
  3. when no route carries attribution, any route that completes, without commission (D4).
- Which routes work, and which carry attribution, is a stored fact per seller and market, refreshed by probes (D5) and by the outcome of real orders.

### D3. Attribution: our authorized tracking travels in the session that completes the order

- **Affiliate networks we have joined.** On the browser route, the session enters the merchant through the network's tracking link generated for our account, with **our click id as the sub-id** (ADR-025 D2). The order reference is linked to that click id inside Pivota. The network's click id therefore lands in the same session that completes the order.
  - Only for programs whose terms, or the merchant in writing, allow orders an agent places on a buyer's behalf. That permission is recorded per program, like ADR-027 Q13.
  - Exactly one tracking click per buyer checkout attempt. Never a tracking click without a buyer's order attempt behind it, never one from a probe (D5), and never one to refresh a cookie.
  - **The session carries ADR-027 Q14's code rules for that program:**
    - only the codes Q14 allows for it (listed for our account, in date, ours or open to all publishers);
    - never a code from the merchant's own site in a program's tracked session;
    - a program's feed code MUST be tried only in that program's tracked session, never in an untracked or unattributed completion: a network can credit an order by its code alone (ADR-027 Q9, Q12);
    - a program that excludes coupon or deal publishers gets the tracked session with no code.
- **Protocol route.** Attribution counts only where the protocol or the network carries it to the order: an attribution field the merchant records, or a network conversion interface that accepts our click id. It is checked per merchant or program before a route is marked as carrying attribution.
- **Agentic affiliate networks:** treated the same way. A recorded program with terms that allow agent-placed orders.
- **Our click id on the order.** On routes where the merchant's order can carry it, the order carries our click id (ADR-025 D2). On every route, Pivota's own order record links the order to the click id.
- The first orders on each program are checked by hand: the network's report must show our sub-id on them. A program that shows a miss goes back to "no attribution" (D4) until a person reviews it.

### D4. No route carries attribution: complete the order, then fix the loop

- The buyer's order comes first: Pivota completes it without commission (peng, Q1 below).
- The order is recorded as **unattributed**, with why: no program for this merchant, the program does not allow agent-placed orders, the route drops the tracking, or the network did not credit it.
- An unattributed order opens a task for a Pivota employee to fix the commission loop for that merchant: join its program, ask permission for agent-placed orders, or set up conversion reporting.
  - The task names the merchant, the market and the reason. It carries **no buyer data**.
  - There is one open task per merchant and market; later orders add to its count.
  - It stays open until the loop is fixed or a person closes it.
- Metric: unattributed completed orders per merchant, reviewed weekly.

### D5. Identity, the browser route and probes

- Every completion request MUST be signed with Pivota's agent identity: protocol signatures (UCP signed and token tiers) and, on the browser route, the card networks' agent recognition (Trusted Agent Protocol, Agent Pay, HTTP message signatures).
- **When the browser route may run (the run condition).** It MUST run on a merchant only when one of these holds:
  - the merchant, or its edge, **verifies** our signature (verified recognition, observed and recorded);
  - the merchant's written permission;
  - a protocol grant that the merchant's protocol terms explicitly extend to browser completion. A protocol grant authorizes that protocol's own route; it is not permission for the browser route unless those terms explicitly cover browser completion.

  Not being challenged is **not** permission. Until the owner decision on robots and terms below is made, the stricter interim rule applies.
- **What the browser route MUST NOT do:**
  - solve a CAPTCHA, rotate addresses, or disguise its user agent or fingerprint;
  - continue after any CAPTCHA, bot challenge or block, or any 3-D Secure or other issuer step-up raised **during the merchant's checkout**. It MUST stop. It MUST NOT route the challenge to a solver, to a person, or to the buyer. (The card program's own buyer approval of a payment, before the checkout, belongs to the enrolment and is not such a challenge.);
  - create an account, sign in, or use a buyer's password. Guest checkout only.
- After a challenge or block, that merchant is not completable on the browser route. It is re-enabled only on **evidence**, never on elapsed time: verified recognition, or the merchant's written permission.
- A merchant that opts out MUST NOT be completed on either route.
- **Probes,** which establish D1's and D2's facts. A probe:
  - MUST run as a background job only;
  - MUST leave by the crawl egress, paced jointly with every other crawl job;
  - MUST stay under a per-host daily cap counted in the database;
  - MUST NOT follow a tracking link or make a payment;
  - MUST NOT run on a merchant on the opt-out denylist;
  - on the browser route, MUST run only on a merchant that meets the run condition above. Elsewhere a probe reads only the merchant's protocol profile.
- Attempts per real order MUST be capped.
- **Open owner decision: robots and terms.** Storefront `robots.txt` defaults usually disallow cart and checkout paths, and store terms may restrict automated access. The decision owner decides, before the browser route takes its first order, whether a buyer-initiated, signed agent completing that buyer's own order is treated as outside `robots.txt` (which governs crawlers), and how store terms are checked. **Until then, the browser route MUST run only under the merchant's written permission or a protocol grant whose terms explicitly cover browser completion (see the run condition above); verified recognition alone is not enough.**

### D6. Money and card data: Pivota never handles the card credential (amended 2026-11-08)

> **Replaced 2026-11-08 (amendment 1).** The 2026-11-02 text let Pivota hold the token number, cryptogram and dynamic security code in memory for one payment attempt, and left open how the credential reaches the checkout (protocol handler, payment provider pass-through, or entry by Pivota's browser). The decision owner decided instead that the payment partner fills the merchant's checkout and Pivota never handles the credential. The earlier text is in this file's history.

- **ADR-016 holds as written.** The merchant's payment provider charges. Pivota never receives, holds, pools or settles funds, and takes no cut of them. The merchant is the merchant of record (D10).
- **The credential is the buyer's.** The agent payment token is the buyer's own credential, issued under the buyer's enrolment with the card network, through the payment partner. The buyer is the cardholder and the merchant's customer. Pivota never pays from funds of its own.
- **The payment partner fills the merchant's checkout.** On the browser route, the partner places the order on the merchant's checkout and uses the credential on its own side. Pivota sends the partner the order: the merchant's checkout or cart, the items, the codes D8 allows, our attribution, and the total the buyer confirmed (D7) as a ceiling the partner MUST NOT exceed. Pivota then reads the outcome: the status, the merchant's order number, the total charged and the currency.
- **Pivota never sees, receives or holds the card credential.** No card number, device or network token number, cryptogram or dynamic security code, in any Pivota process, log, trace, telemetry, screenshot or store. That holds for every route and every partner:
  - Pivota MUST NOT call a step, of a partner or of a card network, whose answer carries a credential, even when Pivota would discard it, and even when it is encrypted;
  - a partner is admitted only when it confirms in writing that it sends no credential to Pivota, and its sandbox answers show none;
  - Pivota's clients still strip any credential-shaped field from a partner answer and record each one as a breach of the partner's contract. Stripping is a detector, not compliance: the rule is met only when nothing arrives;
  - only non-sensitive references are kept: the partner's card or enrolment id, the card brand and the last four digits when the partner supplies them.
- **Protocol route.** Only through a payment handler where no credential passes through Pivota (D2).
- **Browser artifacts** (screenshots, traces, recordings, page dumps) MUST be off on the payment and contact steps, on any runner Pivota operates.
- **Buyer contact and delivery data** MUST NOT be kept beyond the order record.
- **Card-data scope.** Keeping credentials out of Pivota's systems is the precondition for the SAQ-A scope; it is not proof of it. A QSA scoping opinion is still to be obtained, and no SAQ-A claim relies on this ADR until it is on file.

### D7. The buyer confirms exactly what is paid

- **Every code attempt finishes before confirmation.** Before any payment, the agent front shows what the merchant's checkout itself returned, after all code attempts (D8): the seller of record, the items, the codes and automatic discounts applied, shipping, tax, the currency and the total.
- **The confirmation is a cart mandate** under the buyer's consent (ADR-012-agent-transaction-rail-boundary). It binds the seller of record, the items, the currency and the total.
- Payment MUST be for **exactly** that mandate.
- **Any change re-asks:** tax, shipping, a code applied or rejected, an item, the currency or the total. Payment MUST NOT proceed on any change without a new confirmation.
- **Above the consent's spending ceiling, payment MUST stop.** That needs a new consent, not just a re-confirmation.
- **An over-capture** (the merchant charging more than the confirmed total) is a failure. It is alerted and handled as an incident.
- The buyer's contact and delivery details are entered in the merchant's checkout only with the buyer's explicit consent for that order.

### D8. Deal codes on this lane

- "Best" means the code that gives the **lowest buyer total**, never the highest commission.
- **Which codes Pivota tries.** It tries the best code from ADR-027's D2 sources, following ADR-027 Q15:
  - verified codes first;
  - an unverified code, or one whose eligibility is unknown, may be tried in the buyer's own checkout;
  - an eligibility-limited code MUST NOT be tried;
  - in a program's tracked session, only the codes D3 allows.
- **A tried code is not evidence for anyone else.** It never makes a deal `checkout_verified`, and never feeds what is displayed to other buyers.
- A code the checkout rejects is removed before confirmation. After a code is dropped following confirmation, payment MUST NOT proceed without a new confirmation (D7).

### D9. Switches and failure handling

- Flags, all default off:
  - completion at all;
  - each route;
  - each market;
  - an allowlist of merchants for the browser route.
- D1's serving rule is a separate flag, switched on per market only after the completion success rate has been measured. Until then, recommendations never prefer offers that cannot actually be completed.
- After a payment is authorized, the merchant's order state is the truth. A payment is never retried blindly: each attempt is idempotent, keyed by our order reference. A decline stops (D1).
- Every attempt records the route, the outcome and, on failure, the reason, without card data (D6). The "completable" facts (D1) and the route choice (D2) learn from real orders.
- **Refunds and disputes.** On merchants not connected to Pivota, the merchant's payment provider refunds the buyer's card. Pivota never issues, funds or receives a refund. Disputes are between the card issuer and the merchant; Pivota supplies its consent and receipt record. Pivota's after-sales flow relays the buyer's request to the merchant; it executes a refund only where the merchant is connected and the refund runs through that merchant's own payment provider.
  - *Superseded 2026-11-08 by D11:* Pivota executes no refund on any merchant, connected or not. The connected-merchant exception is withdrawn.

### D10. The merchant is merchant of record on every lane (added 2026-11-08)

- On every lane (click-out, prefilled cart, storefront hand-off, the protocol route, the payment partner's route, and the orchestrated checkout of connected merchants) the merchant is the merchant of record and the seller the buyer pays.
- **Pivota is never the payee** of a buyer's payment. No buyer funds settle to an account Pivota owns or controls, on any rail or partner.
- **The payee is set per merchant.** A payment partner's order MUST name the merchant as payee, or the partner's written funds flow MUST settle that merchant's sales to that merchant. A route whose funds would settle to a platform default account, to the partner as seller, or to Pivota MUST NOT be used. Pivota MUST NOT dispatch an order to a partner whose written funds flow (acquirer, merchant of record, settlement to the merchant) is not recorded.
- **What the buyer sees.** The confirmation (D7) names the merchant as the seller. A message Pivota sends about the order says it is sent on the merchant's behalf.
- **Pivota's own revenue** (commission, revenue share) settles to Pivota from networks or partners after the sale (ADR-025 D5). It is never deducted from or routed through the buyer's payment.

### D11. A refund is a request to the merchant (added 2026-11-08)

- A buyer's refund, return or cancellation of a placed order is a **request** that Pivota relays to the merchant, or to the payment partner where the merchant handles after-sales through the partner. The merchant decides it and executes it on its own payment provider.
- **Pivota never executes, issues, funds or receives a refund,** on any lane, including connected merchants whose payment provider Pivota orchestrates. This replaces the connected-merchant exception in D9.
- Before a merchant order exists, Pivota may ask the payment partner to cancel its own attempt and release the authorization. That is a cancellation of an attempt, not a refund.
- Pivota records the request, the merchant's answer and every refund the merchant, its platform or the partner reports (ADR-025 link 4), and reverses attribution and commission accordingly.
- Agent-facing after-sales tools answer with the request and its state (for example `requested`, with a case id and the merchant's contact). They never report a refund that Pivota itself produced.
- Disputes are between the card issuer and the merchant. Pivota supplies its consent and receipt record.

## Phases

- **Phase A, one route, one pilot merchant, one market.**
  - The protocol route, where a pilot merchant's protocol supports completion at a tier we hold.
  - Or the browser route, once the card-network enrolment gives us the buyer's agent token and a credential path, and D5's permission holds for the pilot merchant.
    - *Amended 2026-11-08 (D6):* the payment partner's browser route, once the partner fills the merchant's checkout itself, its written confirmation that it sends Pivota no credential is recorded, its written funds flow names the merchant as payee (D10), and D5's permission holds for the pilot merchant.
  - Orders are placed by Pivota staff as real buyers, with their own cards. Each is checked by hand: the mandate confirmed, the code applied, attribution present, the merchant order received, no card data stored.
- **Phase B, attribution.** Per program: permission for agent-placed orders recorded, first orders checked in the network's report (D3). The unattributed-order task flow (D4) is live before the first unattributed order.
- **Phase C, serving.** D1's serving rule per market, after the completion success rate has been measured.

## Open dependencies (facts and enrolments, not decisions)

- The card-network enrolment: our role in each agent program, and the credential path (D6).
  - *Amended 2026-11-08:* the credential path is decided: the payment partner's, never Pivota's (D6). Still open per partner: its written funds flow (D10), its refund and cancellation process (D11), and the QSA scoping opinion (D6).
- Per program: whether agent-placed orders are allowed, and whether the network reports our sub-id on them (D3).
- Per protocol: whether completion carries attribution, and at which trust tier completion is allowed.
- Which merchants verify our agent signature on the browser route (D5).
- The owner decision on robots and terms (D5).

## Consequences

- Recommendations change: within one product, completable offers win over click-outs, including at a higher price.
- `/r` click-outs become the fallback for products with no completable offer.
- Attribution moves from the buyer's browser into the session Pivota completes. That makes it observable per order, and makes per-program permission for agent-placed orders a hard requirement.
- A new operations loop: unattributed orders become tasks to fix the merchant's commission loop.
- ~~Card-data scope stays SAQ-A by construction (D6).~~ *Replaced 2026-11-08:* card credentials stay out of Pivota's systems (D6), which is the precondition for the SAQ-A scope. A QSA scoping opinion is still to be obtained before any SAQ-A claim relies on it. The enrolment options are limited accordingly.
- *Added 2026-11-08:* the payment partner's token step stays refused in production; Pivota's own credential retrieval from a card network is out of scope; refunds are request-only (amendment 1).
- Coverage depends on merchants recognizing agents. Where they do not, the product falls back to click-outs; Pivota never forces its way through.

## Alternatives considered

- **Keep click-outs as the main lane.** Rejected: the buyer leaves at the moment of decision, and code coverage before a guest checkout is the conversion risk ADR-027 Q11 names.
- **Protocol route only.** Rejected as the only route: too few merchants support completion today. It stays the preferred route at an equal buyer total, where it carries attribution.
- **A browser route that works around bot detection or challenges.** Rejected: it would impersonate a person and evade the merchant's controls. Only recognized, signed agents; stop on every challenge.
- **Pivota as merchant of record, or holding funds.** Rejected by ADR-016, and by D10 on every lane.
- **Pivota completes the payment step, holding the token number, cryptogram and dynamic security code in memory for one attempt** (the 2026-11-02 D6). Rejected 2026-11-08: it puts the card credential in Pivota's process, which brings Pivota's systems into card-data scope, and needs a browser runner of Pivota's own entering credentials.
- **Pivota passes a credential, encrypted to the merchant's key, through unread.** Not taken 2026-11-08: Pivota would still receive the credential, and few merchants have such a key.
- **The payment partner acquires for its own merchants**, so the credential never leaves the partner. Compatible with D6 and D10 where the merchant stays merchant of record, but it covers only merchants on that partner. It stays a fallback to ask the partner about, not the plan.

## Resolved questions (peng, 2026-11-02)

1. **No route carries attribution:** complete the order without commission first, then notify a Pivota employee to fix the commission loop (D4).
2. **A completable offer costs more than a click-out offer for the same product:** the completable offer always wins (D1). This overrides ADR-027 Q6 between lanes, for one product. Among completable offers, Q6 still orders them.
3. **Commission:** through affiliate networks we join, with our authorized tracking in the checkout session, or through agentic affiliate networks (D3).
4. **Routes:** consider both the protocol route and the browser route, whichever is available, provided it can carry our attribution; when neither can, Q1 applies (D2).
5. **Leaving the agent front:** the buyer is not sent to the merchant unless no offer for the product can be completed by Pivota. Where a completable offer for a product exists, click-outs for that product are not recommended (D1).

## Resolved questions (decision owner, 2026-11-08)

6. **Who touches the card credential:** the payment partner fills the merchant's checkout. Pivota never sees, receives or holds the credential (D6, replaced).
7. **Merchant of record:** the merchant, on every lane. Pivota is never the payee (D10).
8. **Refunds:** a request to the merchant, never executed by Pivota (D11).
