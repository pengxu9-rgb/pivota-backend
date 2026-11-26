# ADR-035: Several items from one store in one purchase, only once a rail's quote names every line

**Status:** Proposed, 2026-11-25. It records two owner decisions of 2026-11-25 (see "Owner decisions"): per-line quotes for one store, and several stores as several purchases (amendment 1, D6). Nothing here is built or switched on by this ADR. Until a rail meets D1, every purchase on that rail stays one line, exactly as today.

**Decision owner:** Founder. **Runtime owners:** the backend's agent-purchase code owner (both rails, the unified ledger and the views) and the AgenPas gateway's code owner (the `start_purchase` tool, the buyer page), to be named at acceptance.

## Related

- **ADR-028 (proposed), D7:** the confirmation is a cart mandate that binds the seller of record, the items, the currency and the total. Today "the items" is always one line.
- **ADR-029 (proposed):** one payment router; `amount_ok` is the router's per-rail amount gate.
- **ADR-030 (proposed):** the protocol-native platform. `services/reap_cart_link.py` accepts one cart shape only; the partner's quote "returns the merchant's items, discount, shipping, tax and total".

## Context

A buyer's agent can open a purchase for ONE product line today: one product, one variant, a quantity of 1 to 10 (`MAX_QUANTITY`). The buyer page and its design show "several items" as a later need. Nothing in production depends on multi-line carts yet.

### What is built (origin/main, 2026-11-25)

Every layer of the purchase flow assumes one line.

- **The rail rows store one line as scalar columns.**
  - `reap_agentic_purchases` (migrations 224, 229, 258): `product_key`, `variant_key`, `product_name`, `variant_title`, `quantity`, `our_price_minor` (the unit price), `reap_variant_id`, the unit-price witness columns (`live_unit_price_minor`, `price_rebound_*`) and one `cart_url` (cart-link lane).
  - `a2a_rail_purchases` (migration 268): `checkout_url` (one cart permalink), `product_key`, `product_name`, `variant_title`, `quantity`, `our_price_minor`. There is no variant key.
  - The pilot-scope claims (`db/reap_agentic_ledger.py`) filter on one product, variant and quantity, and on `our_price_minor <= max_total_minor / quantity`. `reap_agentic_eligibility` is keyed per product and variant.
- **The request and its idempotency fingerprint are one line.** `StartPurchaseRequest` (`routes/agent_commerce_reap.py`, shared by the unified door in `routes/agent_commerce_purchases.py`) takes one product selector, one variant selector, one quantity and one `expected_unit_price_minor`. `_request_hash` hashes those facts. The gateway's `start_purchase` tool schema (`mcp-server/src/orchestratedPurchaseTool.js`) says "Buy one product".
- **The cart link is one line by rule.** `services/reap_cart_link.py` refuses a comma in the cart path (`multiple_lines`), `_CART_PATH_RE` matches `/cart/<variant>:<qty>` only, and `cart_link_line` returns one `(variant, quantity)` that about six callers compare with the row's scalar columns. `build_shopify_cart_permalink` builds one line. Both rails hand this URL on as received.
- **The price check is safe only because there is one line.** This is the assumption that decides the design.
  - Reap's quote answer carries no `items` (the published schema for `POST /agentic/quotes` 200 is `id, shippingOptions, amountBreakdown, expiresAt`, and a live sandbox quote of 2026-09-18 agrees). `verify_quote` therefore checks the aggregate `subtotal == unit x quantity` (`services/reap_agentic_purchase.py`, check (b)): with one line, that pins the variant's price.
  - The A2A rail's VIC quote answers an aggregate `items_minor` only. `services/payment_orchestration/vic_order.py` says so in its own words: the check against unit x quantity "is the whole check only because there is one line. A multi-line cart would need the quote to echo each line."
  - The price corroboration derives one unit price from the subtotal (`subtotal // quantity`) and compares it with an independent read of the ONE cart variant.
  - With two or more lines, an aggregate match no longer pins each line: a store (or a mistake in it) can swap one variant for another, or change two line prices against each other, and still match the total. Nothing in the flow would notice.
- **Totals and ceilings are unit x quantity.** The A2A `checkout_ceiling`, `amount_ok` ("OUR price x quantity"), `vic_order.items_amount`, the Reap create `total_minor`, and the buyer page's "Price for N items".
- **The views and the page are one line.** `PUBLIC_PURCHASE_COLUMNS` (Reap), `_PUBLIC_COLUMNS` (A2A), the gateway's `view.js` allowlist (`product_name`, `variant_title`, `quantity`, `amounts.unit_price_minor`) and `app.js` (one item card). The product image (`services/payment_orchestration/purchase_product_image.py`, when it lands) is one image per purchase.
- **What already handles lines.** Reap's quote REQUEST takes `items[1..20]` on its discovery lane (`build_quote_items`), though our caller always sends one. `checkout_amounts.py` (the confirm-card shapes) sums `quantity x price` over `line_items[]` against an aggregate ceiling.

## Owner decisions (2026-11-25)

### 1. One store: "Wait for per-line quotes."

**"Wait for per-line quotes."** Multi-item carts are built only for a rail whose quote names every line. Until then every purchase on that rail stays one line. Ruled out on the same day:

- one page that opens one single-line purchase per line: today's check per line, but one store order per line, shipping possibly charged per order, and lines that can fail one by one;
- one checkout checked on the items total only: the gap the single-line design exists to close.

### 2. Several stores: one purchase per store (amendment 1)

The owner accepted six recommendations on the same day. They are D6 below:

1. a basket from several stores is several per-store purchases tied by a group, never one charge across stores;
2. this amendment, in this ADR;
3. each store stands alone; no approval is shown until every store in the group has a quote, and the page says up front that the basket is several separate orders and charges;
4. at most 5 stores per group;
5. timing: the group and its page are not built ahead of the single-line path's first supervised order;
6. the partner questions below (1 to 14) go out together, after this ADR is accepted, each send with its own go.

What was ruled out above, one purchase per *line* of one store, is not this. Across stores, separate orders, shipping per store and stores that refuse on their own exist whatever Pivota builds.

## Decision

### D1. A rail may carry several lines only once its quote names every line

A rail is **multi-line capable** only when, for a cart of N lines (2 <= N <= the cap of D3), the rail's quote (or its checkout answer, before any charge) returns for EVERY line:

- the variant, in an identifier we can compare with ours (Reap's `variantId`; on a cart link, the Shopify numeric variant);
- the quantity;
- the line's price (unit or line total, and which one it is);

and no line we did not send. A rail that answers only an aggregate, drops a line silently, or substitutes a variant is not capable. Capability is a fact proven per rail and per lane (Reap discovery, Reap external checkout, the A2A rail), recorded in this ADR's "Rail capability" table when the partner answers and a sandbox run confirm it, and switched on per lane by a dial that defaults off.

D1 governs purchases a payment partner places on a store's checkout from our cart link. It does not change the checkout-session tools (UCP, ACP, `create_checkout_session`), which already take up to 50 lines from one store.

### D2. The check is per line, and the whole purchase passes or fails

- **Before the quote:** every line resolves and prices exactly as one line does today (the same `row_*` refusals), against ONE `merchant_domain`. One line that fails refuses the whole purchase with that line's reason and its line number. There is no partial purchase in v1.
- **At the quote:** the echoed lines must equal ours as a set keyed by variant: the same variants, the same quantities, and each line price equal to our unit price x quantity (as `verify_quote` (b) requires of one line today). The aggregate subtotal must still equal the sum. Any difference refuses with `quote_lines_mismatch` and names the line.
- **Price changes:** a changed line price is that line's `price_changed`, judged by today's corroboration rule per line. The rebound logic (`subtotal // quantity`) is replaced by the echoed line price.

### D3. Data, request and fingerprint

- **Lines live in one child table** (`agent_purchase_lines`, keyed by `(rail, rail_purchase_id, line_no)`), not in JSON on the rail rows: each line keeps today's columns (`product_key`, `variant_key`, `product_name`, `variant_title`, `quantity`, `our_price_minor`) and its own refusal code. The rail rows keep their scalar columns, filled from line 1, so every existing reader keeps working on a one-line purchase. A one-line purchase writes one line row too, so readers can move to lines one at a time.
- **The request gains `items[]`** (1 to 20 lines, the bound Reap's discovery lane publishes; one `merchant_domain` for all; each line has today's product selector, variant selector, quantity and `expected_unit_price_minor`). The single-line fields stay as the one-line form, and a request may not carry both.
- **The fingerprint of a one-line request is byte-identical to today's**, whichever form it arrives in. A multi-line request hashes its lines sorted by (product, variant), so the order an agent lists them in does not make a new attempt.
- **Totals become sums.** `amount_ok`, the A2A ceiling, `vic_order.items_amount`, the pilot-scope claim and the Reap create total all take the sum of `our_price_minor x quantity` over the lines.

### D4. The cart link and the consent

- The cart permalink becomes `/cart/<v>:<q>,<v>:<q>` (Shopify's multi-line form) only on a lane that D1 found capable. `reap_cart_link` keeps refusing a comma on every other lane, and keeps refusing two lines for one variant.
- The approval consent binds the full line set (a digest of the sorted lines), not only the purchase: a changed line after consent is a new approval. ADR-028 D7's "the items" then means every line.

### D5. The view and the buyer page

- The views gain `lines[]` (cap 20), each with `product_name`, `variant_title`, `quantity` and the line price, labelled as our offer price unless the store's quote confirmed it. The single-item fields stay, filled from line 1 when there is exactly one line, so a one-line purchase reads exactly as today.
- The buyer page lists the lines (collapsed after three on a phone, "and N more"), with one image per line where `purchase_product_image`'s rule finds one.
- Webhooks and events carry `lines[]` through the unified body, as they carry the view today.

### D6. Several stores are several purchases (amendment 1)

- **One purchase per store.** A basket that names two or more stores becomes one purchase per store. Each is a complete purchase under D1 to D5: its own lines (one line until its rail meets D1), its own quote, approval, token, order and refunds.
- **Nothing spans stores.** No purchase, charge, approval, token or cart mandate covers two stores. This restates ADR-028 D7, ADR-030 D1 and ADR-031 D7; it does not change them. No agentic checkout protocol spans merchants, and an agent network token is scoped to one merchant and one amount.
- **A group ties them, and only shows them.** A new table `agent_purchase_groups` (group id, agent, buyer reference, created at, idempotency key). The parent `agent_purchases` gains a nullable `group_id`. A purchase joins a group at create time only, for the same agent and buyer, and never moves. The group holds no money, amount, consent or order.
- **The request.** `start_purchase` and `POST /agent/v2/commerce/purchases` gain an optional `group_ref`, supplied by the agent. The first purchase that names it creates the group. The group ref is not part of a purchase's fingerprint: a one-store purchase hashes exactly as today. A replay that names a different group is refused (`group_conflict`).
- **The read.** A new `get_purchase_group` returns `purchases[]`, each the unified purchase view as today. The group's status is derived from its purchases, never stored.
- **Each store stands alone.** A refusal, failure, expiry or refund at one store never cancels or changes another. No approval is shown until every store in the group has a quote. Before the first approval, the page says the basket is N separate orders with N charges.
- **A cap.** At most 5 stores per group. A sixth is refused (`group_store_cap`).
- **The page.** One group page lists the stores, each with its total and its own approve step. v1 approves store by store. One approval for all stores needs a yes to questions 11 and 13 and a later amendment.

## Questions for the partners (not sent; to send once this ADR is accepted)

Every answer is checked in a sandbox run before a lane is marked capable.

**Reap, discovery lane (`POST /agentic/quotes` with `items[]`):**

1. With two or more `items`, can the quote answer return each line (`variantId`, `quantity`, unit or line price)? If it can only on request, what is the request?
2. If one variant is unavailable or its price changed, does the quote fail, drop the line, or change it? Is that signalled?
3. Does the order (and its status and refunds) keep lines, and can a line be fulfilled, cancelled or refunded alone?
4. What is the line cap, and is it per quote or per order?

**Reap, external checkout lane (a cart URL):**

5. Is a multi-line Shopify cart permalink accepted as `externalCheckout.checkoutUrl`, and does the quote then name every line?
6. Is the cart read once at quote time, or again at checkout? If again, what happens when the store's cart differs?

**The A2A rail's provider (the checkout skill and the VIC order and quote):**

7. Does the checkout skill accept a multi-line cart URL, and does it confirm every line before the charge?
8. Can the VIC quote answer echo each line (variant, quantity, line price) and not only `items_minor`?
9. Does the cart mandate bind each line, and what does the network check against it?
10. Partial availability and partial fulfilment: refused, dropped, or a partial order?

**Several stores (D6), Reap:**

11. Each checkout gets its own hosted approval page. Can one buyer approve two or more checkouts, one per store, on one page or with one passkey?
12. Can two or more checkouts on the same enrollment be open at the same time, each with its own single-use credential and amount?

**Several stores (D6), the A2A rail's provider:**

13. Can token-setup mint several single-use tokens on one payment method at once, each bound to its own order and amount, under one buyer approval?

**Several stores (D6), both:**

14. The quote's lifetime is the approval window. Can quotes for several stores be held open together while the buyer reviews all of them? If not, what is the longest window?

D6 works if 11 and 13 are answered "one approval per purchase": the page then shows one approve step per store.

## Open decisions (at acceptance)

1. **Partial availability.** v1 refuses the whole purchase (D2). Should a later version offer the buyer the available lines as a new approval?
2. **What "order placed" says when the store fulfils part.** It depends on question 3 and 10.
3. **Counting on the page.** The design's "4 items" counts units while "and 2 more" counts lines. Pick one wording for both.
4. **The line cap.** 20 is the published Reap bound; the page may want fewer.

## Phases

1. **Partner answers.** Send the questions; record the answers and a sandbox run per lane in "Rail capability".
2. **Lines under the hood, one line still.** The child table, the `items[]` request, the byte-identical fingerprint, the summed totals, the views' `lines[]` with one line. No behaviour changes: every purchase is still one line.
3. **One capable lane, dark.** The per-line quote check (D2), the multi-line cart link where it applies (D4), the consent digest, behind that lane's dial, off by default. A staging run end to end.
4. **The page.** The buyer page lists lines; the gateway's `start_purchase` tool takes `items[]` for that lane only.
5. **On.** Per lane, with the owner's go, after the staging run.

**D6, the group,** is independent of phases 1 to 5 and needs no partner answer: the group table, `group_ref`, `get_purchase_group` and the group page, behind a dial off by default. It is not built ahead of the single-line path's first supervised order (owner decision 2, point 5).

## Rail capability

| Lane | Quote names every line | Evidence | Dial |
|---|---|---|---|
| Reap discovery | Unknown (the 2026-09-18 sandbox quote carried no `items`) | none yet | not built |
| Reap external checkout | Unknown | none yet | not built |
| A2A rail (VIC) | No today (`items_minor` only) | `vic_order.py` | not built |

## Consequences

- No purchase gets a weaker check than today: a rail that cannot name every line never carries two.
- Multi-item carts wait on the partners; the work starts with sending ten questions, not with code.
- Phase 2 is safe to build ahead (one line still), and shortens the path once a partner says yes.
- Several stores never wait on a partner: each store is today's purchase, and the group only shows them together. The cost is one approval per store until a partner allows one approval for several.

## Alternatives considered

- **One charge across several stores** (ruled out by the owner, 2026-11-25, decision 2): no agentic checkout protocol spans merchants, an agent token is locked to one merchant and amount, and someone would have to charge once and pay out, which makes Pivota or a partner the merchant of record (ADR-016, ADR-030 D1).

- **One purchase per line under one page** (ruled out by the owner, 2026-11-25): keeps today's check per line, but splits one basket into several store orders, with shipping per order and lines that fail one by one.
- **One checkout checked on the total only** (ruled out by the owner, 2026-11-25): a swap between lines at the same total goes uncaught.
- **Lines as JSON on the rail rows:** fewer tables, but no per-line refusal code, no index per variant, and every reader would parse JSON. The child table keeps lines queryable and leaves the rail rows as they are.
